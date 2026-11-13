from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from threading import Lock
from typing import Literal
from uuid import UUID

import requests
from fastapi import Request
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request as AuthRequest
from google.oauth2 import service_account

from app.core.config import Settings
from app.core.exceptions import AppError


@dataclass(frozen=True)
class PushSendResult:
    status: Literal["ACCEPTED", "INVALID_TOKEN", "RETRYABLE", "FAILED", "UNKNOWN"]
    error_code: str | None = None
    retry_after_seconds: int | None = None


class _AuthRequest(AuthRequest):
    def __init__(self, session: requests.Session, timeout: float):
        super().__init__(session=session)
        self.timeout = timeout

    def __call__(self, *args, **kwargs):
        kwargs["timeout"] = self.timeout
        return super().__call__(*args, **kwargs)


class PushSender:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._credentials = None
        self._lock = Lock()

    def _access_token(self) -> str:
        config = self.settings
        if not config.firebase_project_id or not config.google_application_credentials:
            raise AppError(503, "PUSH_NOT_CONFIGURED", "푸시 알림 설정이 준비되지 않았습니다.")
        try:
            with self._lock:
                if self._credentials is None:
                    self._credentials = service_account.Credentials.from_service_account_file(
                        config.google_application_credentials,
                        scopes=["https://www.googleapis.com/auth/firebase.messaging"],
                    )
                if not self._credentials.valid:
                    with requests.Session() as session:
                        self._credentials.refresh(
                            _AuthRequest(session, config.push_http_timeout_seconds)
                        )
                token = self._credentials.token
                if not isinstance(token, str) or not token:
                    raise ValueError("Missing access token")
                return token
        except (GoogleAuthError, OSError, ValueError, requests.RequestException):
            # 인증 파일과 공급자 응답에는 비밀값이 들어갈 수 있습니다.
            raise AppError(
                503, "PUSH_AUTH_UNAVAILABLE", "푸시 발송 인증을 확인해 주세요."
            ) from None

    def ensure_ready(self) -> None:
        self._access_token()

    def send(self, token: str, notification_id: UUID, plan_id: UUID) -> PushSendResult:
        try:
            access_token = self._access_token()
        except AppError as exc:
            return PushSendResult("RETRYABLE", exc.code, self.settings.push_retry_delay_seconds)
        message = {
            "token": token,
            "notification": {"title": "짐 챙기기", "body": "차량에 실은 짐을 확인해 주세요."},
            "data": {
                "type": "CARGO_REMINDER",
                "notification_id": str(notification_id),
                "plan_id": str(plan_id),
            },
            "android": {
                "priority": "HIGH",
                "ttl": "300s",
                "notification": {
                    "channel_id": "cargo_reminders",
                    "tag": str(notification_id),
                },
            },
        }
        url = (
            "https://fcm.googleapis.com/v1/projects/"
            f"{self.settings.firebase_project_id}/messages:send"
        )
        try:
            # 인증 갱신과 FCM 전송을 분리하고 POST를 자동 재시도하지 않습니다.
            with requests.Session() as session:
                response = session.post(
                    url,
                    json={"message": message},
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=(self.settings.push_http_timeout_seconds,) * 2,
                    allow_redirects=False,
                )
        except requests.RequestException:
            return PushSendResult("UNKNOWN", "PUSH_DELIVERY_UNKNOWN")
        try:
            body = response.json()
        except ValueError:
            return PushSendResult("UNKNOWN", "PUSH_RESPONSE_INVALID")
        if not isinstance(body, dict):
            return PushSendResult("UNKNOWN", "PUSH_RESPONSE_INVALID")
        if response.status_code == 200:
            name = body.get("name")
            if isinstance(name, str) and name.startswith(
                f"projects/{self.settings.firebase_project_id}/messages/"
            ):
                return PushSendResult("ACCEPTED")
            return PushSendResult("UNKNOWN", "PUSH_RESPONSE_INVALID")
        error = body.get("error")
        if not isinstance(error, dict):
            return PushSendResult("UNKNOWN", "PUSH_RESPONSE_INVALID")
        details = error.get("details", [])
        fcm_codes = (
            {
                detail.get("errorCode")
                for detail in details
                if isinstance(detail, dict)
                and detail.get("@type") == "type.googleapis.com/google.firebase.fcm.v1.FcmError"
                and isinstance(detail.get("errorCode"), str)
            }
            if isinstance(details, list)
            else set()
        )
        if response.status_code == 404 and "UNREGISTERED" in fcm_codes:
            return PushSendResult("INVALID_TOKEN", "PUSH_TOKEN_UNREGISTERED")
        if (response.status_code, error.get("status")) in {
            (429, "RESOURCE_EXHAUSTED"),
            (503, "UNAVAILABLE"),
            (401, "UNAUTHENTICATED"),
        }:
            if response.status_code == 401:
                with self._lock:
                    self._credentials = None
            delay = _retry_after(response.headers.get("Retry-After"))
            return PushSendResult(
                "RETRYABLE",
                "PUSH_TEMPORARILY_UNAVAILABLE",
                max(self.settings.push_retry_delay_seconds, delay),
            )
        if response.status_code in {400, 403, 404}:
            # INVALID_ARGUMENT는 잘못된 메시지일 수도 있어 토큰을 삭제하지 않습니다.
            return PushSendResult("FAILED", "PUSH_REQUEST_REJECTED")
        return PushSendResult("UNKNOWN", "PUSH_DELIVERY_UNKNOWN")


def _retry_after(value: str | None) -> int:
    if value is None:
        return 0
    try:
        if value.isdigit():
            return min(int(value), 86400)
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        return max(0, min(int((date - datetime.now(UTC)).total_seconds()) + 1, 86400))
    except (ValueError, TypeError, OverflowError):
        return 0


def get_push_sender(request: Request) -> PushSender:
    return request.app.state.push_sender
