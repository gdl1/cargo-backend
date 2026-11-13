import secrets
from typing import Annotated

from fastapi import Header, Request

from app.core.exceptions import AppError


def get_current_user_id(request: Request) -> str:
    return request.app.state.settings.demo_user_id


def require_notification_key(
    request: Request,
    key: Annotated[str | None, Header(alias="X-Notification-Key")] = None,
) -> None:
    configured = request.app.state.settings.notification_api_key
    if configured is None:
        raise AppError(503, "PUSH_NOT_CONFIGURED", "푸시 알림 설정이 준비되지 않았습니다.")
    if key is None or not secrets.compare_digest(
        key.encode("utf-8"), configured.get_secret_value().encode("utf-8")
    ):
        raise AppError(401, "NOTIFICATION_UNAUTHORIZED", "알림 요청 인증 정보를 확인해 주세요.")
