from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.exceptions import AppError
from app.db.seed import seed_vehicles
from app.db.session import get_db
from app.models.notification import CargoReminder, PushDelivery, PushDevice
from app.models.plan import Plan
from app.schemas.notification import CargoReminderCreate, DeviceRegistration
from app.services import notifications
from app.services.operations import request_guard
from app.services.push import PushSendResult, get_push_sender

API_KEY = "test-notification-key-32-characters-long"


class FakeSender:
    def __init__(self):
        self.calls = []
        self.results = {}
        self.ready_error = None
        self.ready_calls = 0

    def ensure_ready(self):
        self.ready_calls += 1
        if self.ready_error:
            raise self.ready_error

    def send(self, token, notification_id, plan_id):
        self.calls.append((token, notification_id, plan_id))
        result = self.results.get(token, PushSendResult(status="ACCEPTED"))
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture
def reminder_app(
    application: FastAPI, settings: Settings, model_session: Session
) -> Iterator[SimpleNamespace]:
    settings.notification_api_key = SecretStr(API_KEY)
    seed_vehicles(model_session)
    plan = Plan(user_id=settings.demo_user_id, vehicle_id="avante_demo", passenger_count=1)
    model_session.add(plan)
    model_session.commit()
    plan_id = plan.plan_id
    model_session.commit()

    def override_db():
        yield model_session

    sender = FakeSender()
    application.dependency_overrides[get_db] = override_db
    application.dependency_overrides[get_push_sender] = lambda: sender
    with TestClient(application, headers={"X-Notification-Key": API_KEY}) as client:
        yield SimpleNamespace(
            client=client,
            session=model_session,
            sender=sender,
            settings=settings,
            plan_id=plan_id,
        )


def register(app, token="device-token", device_id=None):
    device_id = device_id or uuid4()
    response = app.client.put(f"/api/v1/notifications/devices/{device_id}", json={"token": token})
    assert response.status_code == 200, response.text
    assert token not in response.text
    return device_id


def remind(app, key=None, payload=None, plan_id=None):
    return app.client.post(
        f"/api/v1/plans/{plan_id or app.plan_id}/cargo-reminders",
        headers={"Idempotency-Key": str(key or uuid4())},
        json=payload
        or {"event_type": "DESTINATION_ARRIVED", "occurred_at": datetime.now(UTC).isoformat()},
    )


def test_token_rotation_replaces_old_token_and_unregister_is_repeatable(reminder_app):
    app = reminder_app
    device_id = register(app, "old-token")
    register(app, "new-token", device_id)
    response = remind(app)
    assert response.status_code == 201
    assert response.json()["data"]["status"] == "ACCEPTED"
    assert [call[0] for call in app.sender.calls] == ["new-token"]
    first = app.client.delete(f"/api/v1/notifications/devices/{device_id}")
    second = app.client.delete(f"/api/v1/notifications/devices/{device_id}")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert second.json()["data"]["active"] is False
    with app.session.begin():
        device = app.session.get(PushDevice, device_id)
        assert device.token is None
        assert device.token_hash is None
    assert remind(app).json()["data"]["status"] == "NO_DEVICES"
    assert len(app.sender.calls) == 1


def test_token_moves_between_same_users_installations_without_double_send(reminder_app):
    app = reminder_app
    first = register(app, "one-token")
    second = register(app, "one-token")
    data = remind(app).json()["data"]
    assert data["accepted_count"] == 1
    assert data["deliveries"][0]["device_id"] == str(second)
    with app.session.begin():
        assert app.session.get(PushDevice, first).active is False
    assert len(app.sender.calls) == 1


def test_other_users_device_and_token_cannot_be_taken_over(reminder_app):
    app = reminder_app
    other_id = uuid4()
    notifications.register_device(
        app.session, "other-user", other_id, DeviceRegistration(token="other-token")
    )
    response = app.client.put(
        f"/api/v1/notifications/devices/{other_id}", json={"token": "replacement"}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "DEVICE_NOT_FOUND"
    assert app.client.delete(f"/api/v1/notifications/devices/{other_id}").status_code == 404
    response = app.client.put(
        f"/api/v1/notifications/devices/{uuid4()}", json={"token": "other-token"}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "PUSH_TOKEN_IN_USE"
    with app.session.begin():
        assert app.session.get(PushDevice, other_id).token == "other-token"


def test_device_limit_allows_rotation_but_rejects_an_eleventh_device(reminder_app):
    app = reminder_app
    devices = [register(app, f"token-{index}") for index in range(10)]
    response = app.client.put(
        f"/api/v1/notifications/devices/{uuid4()}", json={"token": "eleventh-token"}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "DEVICE_LIMIT_EXCEEDED"
    register(app, "rotated-token", devices[0])
    app.client.delete(f"/api/v1/notifications/devices/{devices[1]}")
    register(app, "new-installation")
    assert remind(app).json()["data"]["accepted_count"] == 10


@pytest.mark.parametrize("other_user", [False, True])
def test_non_owned_or_missing_plan_never_sends(reminder_app, other_user):
    app = reminder_app
    register(app)
    plan_id = uuid4()
    if other_user:
        with app.session.begin():
            app.session.add(
                Plan(
                    plan_id=plan_id,
                    user_id="other-user",
                    vehicle_id="avante_demo",
                    passenger_count=1,
                )
            )
    response = remind(app, plan_id=plan_id)
    assert response.status_code == 404
    assert response.json()["code"] == "PLAN_NOT_FOUND"
    assert not app.sender.calls
    assert app.sender.ready_calls == 0


def test_same_event_replays_without_resending_even_after_expiry(reminder_app, monkeypatch):
    app = reminder_app
    device_id = register(app)
    now = datetime.now(UTC)
    key = uuid4()
    payload = {"event_type": "DOOR_OPENED", "occurred_at": now.isoformat()}
    first = remind(app, key, payload)
    monkeypatch.setattr(notifications, "_now", lambda: now + timedelta(days=1))
    app.sender.ready_error = AppError(503, "PUSH_NOT_CONFIGURED", "설정 없음")
    second = remind(app, key, payload)
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json() == second.json()
    data = first.json()["data"]
    assert data["accepted_count"] == 1
    assert data["deliveries"][0]["device_id"] == str(device_id)
    assert app.sender.calls == [("device-token", UUID(data["notification_id"]), app.plan_id)]


def test_event_key_cannot_be_reused_with_another_body_or_plan(reminder_app):
    app = reminder_app
    register(app)
    key = uuid4()
    payload = {"event_type": "DOOR_OPENED", "occurred_at": datetime.now(UTC).isoformat()}
    assert remind(app, key, payload).status_code == 201
    response = remind(app, key, {**payload, "event_type": "DESTINATION_ARRIVED"})
    assert response.status_code == 409
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    with app.session.begin():
        other_plan = Plan(
            user_id=app.settings.demo_user_id, vehicle_id="avante_demo", passenger_count=1
        )
        app.session.add(other_plan)
        app.session.flush()
        other_id = other_plan.plan_id
    response = remind(app, key, payload, other_id)
    assert response.status_code == 409
    assert len(app.sender.calls) == 1


def test_simultaneous_same_event_is_rejected_without_external_call(reminder_app):
    app = reminder_app
    register(app)
    key = uuid4()
    with request_guard(app.session, app.settings.demo_user_id, "cargo-reminder", key):
        response = remind(app, key)
    assert response.status_code == 409
    assert response.json()["code"] == "REQUEST_IN_PROGRESS"
    assert not app.sender.calls
    assert remind(app, key).status_code == 201


def test_failed_device_does_not_hide_success_or_delete_valid_tokens(reminder_app):
    app = reminder_app
    valid_id = register(app, "valid-token")
    invalid_id = register(app, "expired-token")
    bad_payload_id = register(app, "payload-rejected-token")
    app.sender.results = {
        "expired-token": PushSendResult(status="INVALID_TOKEN", error_code="UNREGISTERED"),
        "payload-rejected-token": PushSendResult(status="FAILED", error_code="INVALID_ARGUMENT"),
    }
    response = remind(app)
    data = response.json()["data"]
    assert data["status"] == "PARTIAL"
    assert data["accepted_count"] == 1
    assert {entry["status"] for entry in data["deliveries"]} == {"ACCEPTED", "FAILED"}
    with app.session.begin():
        assert app.session.get(PushDevice, valid_id).active is True
        assert app.session.get(PushDevice, invalid_id).token is None
        assert app.session.get(PushDevice, bad_payload_id).active is True
    assert "valid-token" not in response.text
    assert "expired-token" not in response.text


def test_only_explicit_rejections_retry_and_honor_provider_delay(reminder_app, monkeypatch):
    app = reminder_app
    now = datetime.now(UTC)
    monkeypatch.setattr(notifications, "_now", lambda: now)
    register(app, "accepted-token")
    register(app, "retry-token")
    app.sender.results["retry-token"] = PushSendResult(
        status="RETRYABLE", error_code="UNAVAILABLE", retry_after_seconds=120
    )
    payload = {"event_type": "DOOR_OPENED", "occurred_at": now.isoformat()}
    key = uuid4()
    first = remind(app, key, payload)
    data = first.json()["data"]
    assert data["status"] == "PARTIAL"
    retry = next(item for item in data["deliveries"] if item["status"] == "RETRYABLE")
    assert datetime.fromisoformat(retry["retry_at"]) == now + timedelta(seconds=120)
    assert remind(app, key, payload).json() == first.json()
    assert len(app.sender.calls) == 2
    app.sender.results["retry-token"] = PushSendResult(status="ACCEPTED")
    monkeypatch.setattr(notifications, "_now", lambda: now + timedelta(seconds=120))
    last = remind(app, key, payload)
    assert last.status_code == 200
    assert last.json()["data"]["status"] == "ACCEPTED"
    assert [call[0] for call in app.sender.calls].count("accepted-token") == 1
    assert [call[0] for call in app.sender.calls].count("retry-token") == 2
    assert last.json()["data"]["notification_id"] == data["notification_id"]


def test_retry_stops_when_event_expires(reminder_app, monkeypatch):
    app = reminder_app
    now = datetime.now(UTC)
    monkeypatch.setattr(notifications, "_now", lambda: now)
    register(app)
    app.sender.results["device-token"] = PushSendResult(
        status="RETRYABLE", error_code="UNAVAILABLE"
    )
    key = uuid4()
    payload = {"event_type": "DOOR_OPENED", "occurred_at": now.isoformat()}
    assert remind(app, key, payload).json()["data"]["status"] == "RETRYABLE"
    monkeypatch.setattr(notifications, "_now", lambda: now + timedelta(seconds=601))
    data = remind(app, key, payload).json()["data"]
    assert data["status"] == "FAILED"
    assert data["deliveries"][0]["error_code"] == "EVENT_EXPIRED"
    assert data["deliveries"][0]["retry_at"] is None
    assert len(app.sender.calls) == 1


@pytest.mark.parametrize("result", [PushSendResult(status="UNKNOWN"), RuntimeError("secret-token")])
def test_uncertain_outcome_does_not_resend_or_leak_exception(reminder_app, result, caplog):
    app = reminder_app
    register(app)
    app.sender.results["device-token"] = result
    key = uuid4()
    payload = {"event_type": "DOOR_OPENED", "occurred_at": datetime.now(UTC).isoformat()}
    first = remind(app, key, payload)
    second = remind(app, key, payload)
    assert first.json()["data"]["status"] == "UNKNOWN"
    assert first.json() == second.json()
    assert len(app.sender.calls) == 1
    assert "secret-token" not in first.text
    assert "secret-token" not in caplog.text


def test_crash_after_send_is_recorded_as_unknown_without_resend(reminder_app):
    app = reminder_app
    register(app)
    app.sender.results["device-token"] = SystemExit("process stopped after sending")
    key = uuid4()
    payload = CargoReminderCreate(event_type="DOOR_OPENED", occurred_at=datetime.now(UTC))
    with pytest.raises(SystemExit):
        notifications.send_cargo_reminder(
            app.session,
            app.settings.demo_user_id,
            app.plan_id,
            key,
            payload,
            app.settings,
            app.sender,
        )
    with app.session.begin():
        row = app.session.scalar(select(PushDelivery))
        assert row.status == "SENDING"
    app.sender.results.clear()
    result, created = notifications.send_cargo_reminder(
        app.session, app.settings.demo_user_id, app.plan_id, key, payload, app.settings, app.sender
    )
    assert created is False
    assert result.status == "UNKNOWN"
    assert len(app.sender.calls) == 1


@pytest.mark.parametrize("change", ["rotate", "unregister"])
def test_rotated_or_removed_token_is_not_used_for_pending_retry(reminder_app, monkeypatch, change):
    app = reminder_app
    now = datetime.now(UTC)
    monkeypatch.setattr(notifications, "_now", lambda: now)
    device_id = register(app)
    app.sender.results["device-token"] = PushSendResult(
        status="RETRYABLE", error_code="UNAVAILABLE"
    )
    key = uuid4()
    payload = {"event_type": "DOOR_OPENED", "occurred_at": now.isoformat()}
    assert remind(app, key, payload).status_code == 201
    if change == "rotate":
        register(app, "replacement-token", device_id)
    else:
        assert app.client.delete(f"/api/v1/notifications/devices/{device_id}").status_code == 200
    monkeypatch.setattr(notifications, "_now", lambda: now + timedelta(seconds=61))
    data = remind(app, key, payload).json()["data"]
    assert data["deliveries"][0]["status"] == "SKIPPED"
    assert data["deliveries"][0]["error_code"] == "DEVICE_CHANGED"
    assert len(app.sender.calls) == 1
    if change == "rotate":
        with app.session.begin():
            assert app.session.get(PushDevice, device_id).token == "replacement-token"


@pytest.mark.parametrize("expire_on_commit", [False, True])
def test_invalid_old_token_does_not_remove_token_rotated_during_send(
    reminder_app, expire_on_commit
):
    app = reminder_app
    app.session.expire_on_commit = expire_on_commit
    device_id = register(app, "old-token")

    def rotate_during_send(token, notification_id, plan_id):
        app.sender.calls.append((token, notification_id, plan_id))
        notifications.register_device(
            app.session,
            app.settings.demo_user_id,
            device_id,
            DeviceRegistration(token="replacement-token"),
        )
        return PushSendResult(status="INVALID_TOKEN", error_code="UNREGISTERED")

    app.sender.send = rotate_during_send
    response = remind(app)
    assert response.status_code == 201
    assert response.json()["data"]["status"] == "FAILED"
    assert app.sender.calls[0][0] == "old-token"
    with app.session.begin():
        device = app.session.get(PushDevice, device_id)
        assert device.active is True
        assert device.token == "replacement-token"


def test_no_device_result_remains_stable_after_new_device_registers(reminder_app):
    app = reminder_app
    key = uuid4()
    payload = {"event_type": "DOOR_OPENED", "occurred_at": datetime.now(UTC).isoformat()}
    first = remind(app, key, payload)
    assert first.json()["data"]["status"] == "NO_DEVICES"
    assert app.sender.ready_calls == 0
    register(app)
    second = remind(app, key, payload)
    assert second.status_code == 200
    assert second.json() == first.json()
    assert not app.sender.calls


def test_configuration_failure_rolls_back_reminder_and_can_be_retried(reminder_app):
    app = reminder_app
    register(app)
    key = uuid4()
    payload = {"event_type": "DOOR_OPENED", "occurred_at": datetime.now(UTC).isoformat()}
    app.sender.ready_error = AppError(503, "PUSH_AUTH_UNAVAILABLE", "인증 실패")
    response = remind(app, key, payload)
    assert response.status_code == 503
    with app.session.begin():
        assert app.session.scalar(select(func.count()).select_from(CargoReminder)) == 0
        assert app.session.scalar(select(func.count()).select_from(PushDelivery)) == 0
    app.sender.ready_error = None
    assert remind(app, key, payload).status_code == 201
    assert len(app.sender.calls) == 1


@pytest.mark.parametrize(("seconds", "code"), [(601, "EVENT_EXPIRED"), (-61, "EVENT_TIME_INVALID")])
def test_old_or_future_events_do_not_send(reminder_app, monkeypatch, seconds, code):
    app = reminder_app
    now = datetime.now(UTC)
    monkeypatch.setattr(notifications, "_now", lambda: now)
    register(app)
    response = remind(
        app,
        payload={
            "event_type": "DOOR_OPENED",
            "occurred_at": (now - timedelta(seconds=seconds)).isoformat(),
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == code
    assert not app.sender.calls
    assert app.sender.ready_calls == 0
