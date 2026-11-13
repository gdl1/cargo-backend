from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.session import get_db
from app.services.push import get_push_sender

KEY = "test-notification-key-32-characters-long"


@pytest.fixture
def notification_client(application: FastAPI, settings: Settings):
    settings.notification_api_key = SecretStr(KEY)
    session = Mock(spec=Session)
    sender = Mock()

    def override_db():
        yield session

    application.dependency_overrides[get_db] = override_db
    application.dependency_overrides[get_push_sender] = lambda: sender
    with TestClient(application) as client:
        yield client, session, sender


@pytest.mark.parametrize("provided_key", [None, "wrong-key"])
@pytest.mark.parametrize("method", ["put", "delete", "post"])
def test_notification_auth_is_required(notification_client, provided_key, method):
    client, session, sender = notification_client
    headers = {"X-Notification-Key": provided_key} if provided_key else {}
    if method == "post":
        path = f"/api/v1/plans/{uuid4()}/cargo-reminders"
        headers["Idempotency-Key"] = str(uuid4())
        payload = {"event_type": "DOOR_OPENED", "occurred_at": datetime.now(UTC).isoformat()}
    else:
        path = f"/api/v1/notifications/devices/{uuid4()}"
        payload = {"token": "private-device-token"}
    response = client.request(method, path, headers=headers, json=payload)
    assert response.status_code == 401
    assert response.json()["code"] == "NOTIFICATION_UNAUTHORIZED"
    assert not session.mock_calls
    assert not sender.mock_calls
    assert "private-device-token" not in response.text


def test_missing_server_notification_key_fails_closed(notification_client, settings):
    client, session, sender = notification_client
    settings.notification_api_key = None
    response = client.put(
        f"/api/v1/notifications/devices/{uuid4()}",
        headers={"X-Notification-Key": KEY},
        json={"token": "token"},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "PUSH_NOT_CONFIGURED"
    assert not session.mock_calls
    assert not sender.mock_calls


@pytest.mark.parametrize(
    "payload",
    [
        {"token": ""},
        {"token": "white space"},
        {"token": "line\nbreak"},
        {"token": "x" * 4097},
        {"token": 123},
        {"token": "token", "platform": "IOS"},
        {"token": "token", "user_id": "someone-else"},
    ],
)
def test_invalid_device_registration_is_rejected_before_database(notification_client, payload):
    client, session, sender = notification_client
    response = client.put(
        f"/api/v1/notifications/devices/{uuid4()}",
        headers={"X-Notification-Key": KEY},
        json=payload,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert not session.mock_calls
    assert not sender.mock_calls


@pytest.mark.parametrize(
    "changes",
    [
        {"event_type": "PARKED"},
        {"occurred_at": "2026-11-13T12:00:00"},
        {"occurred_at": "not-a-date"},
        {"user_id": "other-user"},
        {"token": "target-token"},
        {"message": "arbitrary notification"},
    ],
)
def test_invalid_reminder_input_cannot_trigger_sender(notification_client, changes):
    client, session, sender = notification_client
    response = client.post(
        f"/api/v1/plans/{uuid4()}/cargo-reminders",
        headers={"X-Notification-Key": KEY, "Idempotency-Key": str(uuid4())},
        json={
            "event_type": "DOOR_OPENED",
            "occurred_at": datetime.now(UTC).isoformat(),
            **changes,
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert not session.mock_calls
    assert not sender.mock_calls


@pytest.mark.parametrize("key", [None, "not-a-uuid"])
def test_reminder_requires_uuid_idempotency_key(notification_client, key):
    client, session, sender = notification_client
    headers = {"X-Notification-Key": KEY}
    if key:
        headers["Idempotency-Key"] = key
    response = client.post(
        f"/api/v1/plans/{uuid4()}/cargo-reminders",
        headers=headers,
        json={"event_type": "DOOR_OPENED", "occurred_at": datetime.now(UTC).isoformat()},
    )
    assert response.status_code == 422
    assert not session.mock_calls
    assert not sender.mock_calls
