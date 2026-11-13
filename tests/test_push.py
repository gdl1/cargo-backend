from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest
import requests
from google.auth.exceptions import RefreshError
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.core.exceptions import AppError
from app.services.push import PushSender, _AuthRequest, _retry_after


@pytest.fixture
def sender(settings: Settings) -> PushSender:
    settings.firebase_project_id = "cargo-test-project"
    settings.google_application_credentials = "/not-a-real-credential.json"
    result = PushSender(settings)
    result._credentials = Mock(valid=True, token="private-access-token")
    return result


def mock_transport(monkeypatch, status=200, body=None, headers=None):
    response = Mock(status_code=status, headers=headers or {})
    response.json.return_value = body
    session = Mock()
    session.post.return_value = response
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    monkeypatch.setattr("app.services.push.requests.Session", lambda: session)
    return session


def fcm_error(status, code=None):
    return {
        "error": {
            "status": status,
            "details": []
            if code is None
            else [
                {
                    "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                    "errorCode": code,
                }
            ],
        }
    }


def test_notification_contains_only_fixed_text_and_identifiers(sender, monkeypatch):
    session = mock_transport(monkeypatch, body={"name": "projects/cargo-test-project/messages/id"})
    notification_id, plan_id = uuid4(), uuid4()
    result = sender.send("private-device-token", notification_id, plan_id)
    assert result.status == "ACCEPTED"
    session.post.assert_called_once()
    url = session.post.call_args.args[0]
    request = session.post.call_args.kwargs
    assert url == "https://fcm.googleapis.com/v1/projects/cargo-test-project/messages:send"
    assert request["timeout"] == (5.0, 5.0)
    assert request["allow_redirects"] is False
    message = request["json"]["message"]
    assert message["notification"] == {
        "title": "짐 챙기기",
        "body": "차량에 실은 짐을 확인해 주세요.",
    }
    assert message["data"] == {
        "type": "CARGO_REMINDER",
        "notification_id": str(notification_id),
        "plan_id": str(plan_id),
    }
    assert message["android"]["notification"]["tag"] == str(notification_id)
    assert message["android"]["notification"]["channel_id"] == "cargo_reminders"
    assert message["android"]["ttl"] == "300s"


@pytest.mark.parametrize(
    ("http_status", "body", "expected"),
    [
        (404, fcm_error("NOT_FOUND", "UNREGISTERED"), "INVALID_TOKEN"),
        (404, fcm_error("NOT_FOUND"), "FAILED"),
        (400, fcm_error("INVALID_ARGUMENT", "INVALID_ARGUMENT"), "FAILED"),
        (403, fcm_error("PERMISSION_DENIED", "SENDER_ID_MISMATCH"), "FAILED"),
        (429, fcm_error("RESOURCE_EXHAUSTED", "QUOTA_EXCEEDED"), "RETRYABLE"),
        (503, fcm_error("UNAVAILABLE", "UNAVAILABLE"), "RETRYABLE"),
        (500, fcm_error("INTERNAL"), "UNKNOWN"),
        (503, {}, "UNKNOWN"),
        (200, {}, "UNKNOWN"),
        (200, {"name": "projects/other-project/messages/id"}, "UNKNOWN"),
        (200, [], "UNKNOWN"),
        (302, fcm_error("REDIRECT"), "UNKNOWN"),
    ],
)
def test_fcm_errors_do_not_trigger_hidden_retries(sender, monkeypatch, http_status, body, expected):
    session = mock_transport(monkeypatch, http_status, body)
    result = sender.send("device-token", uuid4(), uuid4())
    assert result.status == expected
    assert session.post.call_count == 1
    if expected == "RETRYABLE":
        assert result.retry_after_seconds >= 60


@pytest.mark.parametrize("failure", [requests.ReadTimeout, requests.ConnectionError])
def test_ambiguous_transport_failure_is_not_retried(sender, monkeypatch, failure, caplog):
    session = mock_transport(monkeypatch)
    session.post.side_effect = failure("private-token-in-internal-error")
    result = sender.send("private-token", uuid4(), uuid4())
    assert result.status == "UNKNOWN"
    assert session.post.call_count == 1
    assert "private-token" not in caplog.text


def test_retry_after_is_respected(sender, monkeypatch):
    mock_transport(monkeypatch, 429, fcm_error("RESOURCE_EXHAUSTED"), {"Retry-After": "120"})
    assert sender.send("device-token", uuid4(), uuid4()).retry_after_seconds == 120


def test_auth_rejection_invalidates_cached_credentials_without_resending(sender, monkeypatch):
    session = mock_transport(monkeypatch, 401, fcm_error("UNAUTHENTICATED"))
    assert sender.send("device-token", uuid4(), uuid4()).status == "RETRYABLE"
    assert sender._credentials is None
    assert session.post.call_count == 1


def test_malformed_json_is_unknown(sender, monkeypatch):
    session = mock_transport(monkeypatch)
    session.post.return_value.json.side_effect = ValueError("malformed")
    assert sender.send("device-token", uuid4(), uuid4()).status == "UNKNOWN"


def test_missing_configuration_never_attempts_network(settings, monkeypatch):
    session = mock_transport(monkeypatch)
    result = PushSender(settings)
    with pytest.raises(AppError) as error:
        result.ensure_ready()
    assert error.value.code == "PUSH_NOT_CONFIGURED"
    assert session.post.call_count == 0


def test_auth_refresh_error_is_sanitized_and_prevents_send(sender, monkeypatch):
    session = mock_transport(monkeypatch)
    sender._credentials.valid = False
    sender._credentials.refresh.side_effect = RefreshError("private-service-account-data")
    with pytest.raises(AppError) as error:
        sender.ensure_ready()
    assert error.value.code == "PUSH_AUTH_UNAVAILABLE"
    assert "private" not in str(error.value)
    assert sender.send("token", uuid4(), uuid4()).status == "RETRYABLE"
    assert session.post.call_count == 0


def test_credentials_are_reused(sender):
    sender.ensure_ready()
    sender.ensure_ready()
    sender._credentials.refresh.assert_not_called()


def test_auth_transport_has_explicit_timeout():
    session = Mock()
    transport = _AuthRequest(session, 3.0)
    transport("https://oauth2.googleapis.com/token", method="POST")
    assert session.request.call_args.kwargs["timeout"] == 3.0


@pytest.mark.parametrize("value", [None, "invalid", "-1"])
def test_invalid_retry_after_defaults_to_service_delay(value):
    assert _retry_after(value) == 0


def test_http_date_retry_after():
    future = datetime.now(UTC) + timedelta(seconds=120)
    assert 118 <= _retry_after(format_datetime(future)) <= 121


@pytest.mark.parametrize("key", ["short", "한글" * 32, "a" * 31 + " "])
def test_weak_notification_key_is_rejected(key):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, postgres_password=SecretStr("test"), notification_api_key=key)


def test_blank_notification_key_disables_endpoints():
    config = Settings(_env_file=None, postgres_password=SecretStr("test"), notification_api_key="")
    assert config.notification_api_key is None


def test_project_id_cannot_change_fcm_destination():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, postgres_password=SecretStr("test"), firebase_project_id="x/other")
