from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user_id, require_notification_key
from app.db.session import get_db
from app.schemas.common import ApiResponse
from app.schemas.notification import (
    CargoReminderCreate,
    CargoReminderData,
    DeviceData,
    DeviceRegistration,
)
from app.services.notifications import register_device, send_cargo_reminder, unregister_device
from app.services.push import PushSender, get_push_sender

router = APIRouter(tags=["모바일 알림"], dependencies=[Depends(require_notification_key)])


@router.put(
    "/notifications/devices/{device_id}",
    response_model=ApiResponse[DeviceData],
    summary="모바일 푸시 토큰 등록 및 갱신",
)
def save_push_device(
    device_id: UUID,
    payload: DeviceRegistration,
    session: Annotated[Session, Depends(get_db)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> ApiResponse[DeviceData]:
    return ApiResponse(
        code="SUCCESS",
        message="푸시 토큰을 등록했습니다.",
        data=register_device(session, user_id, device_id, payload),
    )


@router.delete(
    "/notifications/devices/{device_id}",
    response_model=ApiResponse[DeviceData],
    summary="모바일 푸시 토큰 해제",
)
def delete_push_device(
    device_id: UUID,
    session: Annotated[Session, Depends(get_db)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> ApiResponse[DeviceData]:
    return ApiResponse(
        code="SUCCESS",
        message="푸시 토큰을 해제했습니다.",
        data=unregister_device(session, user_id, device_id),
    )


@router.post(
    "/plans/{plan_id}/cargo-reminders",
    status_code=201,
    response_model=ApiResponse[CargoReminderData],
    summary="모바일 짐 챙기기 알림 요청",
)
def remind_cargo(
    plan_id: UUID,
    payload: CargoReminderCreate,
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(get_db)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    sender: Annotated[PushSender, Depends(get_push_sender)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> ApiResponse[CargoReminderData]:
    data, created = send_cargo_reminder(
        session, user_id, plan_id, idempotency_key, payload, request.app.state.settings, sender
    )
    response.status_code = 201 if created else 200
    return ApiResponse(code="SUCCESS", message="알림 요청을 처리했습니다.", data=data)
