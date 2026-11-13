import hashlib
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.exceptions import AppError
from app.models.notification import CargoReminder, PushDelivery, PushDevice
from app.models.plan import Plan
from app.schemas.notification import (
    CargoReminderCreate,
    CargoReminderData,
    DeliveryData,
    DeviceData,
    DeviceRegistration,
)
from app.services.operations import request_fingerprint, request_guard
from app.services.push import PushSender

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _lock_devices(session: Session, user_id: str) -> None:
    digest = hashlib.sha256(f"push-devices:{user_id}".encode()).digest()
    lock_id = int.from_bytes(digest[:8], "big", signed=True)
    if not session.scalar(text("SELECT pg_try_advisory_xact_lock(:id)"), {"id": lock_id}):
        raise AppError(409, "REQUEST_IN_PROGRESS", "기기 정보를 변경 중입니다.")


def _deactivate(device: PushDevice) -> None:
    device.active = False
    device.token = None
    device.token_hash = None
    device.updated_at = _now()


def _device_data(device: PushDevice) -> DeviceData:
    return DeviceData.model_validate(device, from_attributes=True)


def register_device(
    session: Session, user_id: str, device_id: UUID, payload: DeviceRegistration
) -> DeviceData:
    token_hash = hashlib.sha256(payload.token.encode()).hexdigest()
    try:
        with session.begin():
            _lock_devices(session, user_id)
            device = session.get(
                PushDevice, device_id, with_for_update=True, populate_existing=True
            )
            if device is not None and device.user_id != user_id:
                raise AppError(404, "DEVICE_NOT_FOUND", "기기를 찾을 수 없습니다.")
            previous = session.scalar(
                select(PushDevice).where(PushDevice.token_hash == token_hash).with_for_update()
            )
            if previous is not None and previous.device_id != device_id:
                if previous.user_id != user_id:
                    raise AppError(409, "PUSH_TOKEN_IN_USE", "이미 등록된 푸시 토큰입니다.")
                _deactivate(previous)
                session.flush()
            if device is None or not device.active:
                count = session.scalar(
                    select(func.count())
                    .select_from(PushDevice)
                    .where(PushDevice.user_id == user_id, PushDevice.active.is_(True))
                )
                if count >= 10:
                    raise AppError(
                        409, "DEVICE_LIMIT_EXCEEDED", "등록 가능한 기기는 최대 10개입니다."
                    )
            if device is None:
                device = PushDevice(device_id=device_id, user_id=user_id)
                session.add(device)
            device.platform = payload.platform
            device.token = payload.token
            device.token_hash = token_hash
            device.active = True
            device.updated_at = _now()
            session.flush()
            return _device_data(device)
    except IntegrityError as exc:
        raise AppError(
            409, "PUSH_TOKEN_IN_USE", "기기 또는 푸시 토큰이 이미 등록되어 있습니다."
        ) from exc


def unregister_device(session: Session, user_id: str, device_id: UUID) -> DeviceData:
    with session.begin():
        _lock_devices(session, user_id)
        device = session.get(PushDevice, device_id, with_for_update=True, populate_existing=True)
        if device is None or device.user_id != user_id:
            raise AppError(404, "DEVICE_NOT_FOUND", "기기를 찾을 수 없습니다.")
        if device.active:
            _deactivate(device)
        session.flush()
        return _device_data(device)


def _prepare_reminder(
    session: Session,
    user_id: str,
    plan_id: UUID,
    key: UUID,
    payload: CargoReminderCreate,
    settings: Settings,
    sender: PushSender,
) -> tuple[UUID, bool]:
    fingerprint = request_fingerprint({"plan_id": plan_id, "event": payload})
    with session.begin():
        plan = session.scalar(select(Plan).where(Plan.plan_id == plan_id, Plan.user_id == user_id))
        if plan is None:
            raise AppError(404, "PLAN_NOT_FOUND", "적재 계획을 찾을 수 없습니다.")
        reminder = session.scalar(
            select(CargoReminder).where(
                CargoReminder.user_id == user_id, CargoReminder.idempotency_key == key
            )
        )
        if reminder is not None:
            if reminder.request_hash != fingerprint:
                raise AppError(409, "IDEMPOTENCY_KEY_REUSED", "다른 입력에 사용한 요청 키입니다.")
            return reminder.notification_id, False
        age = (_now() - payload.occurred_at).total_seconds()
        if age < -60:
            raise AppError(422, "EVENT_TIME_INVALID", "이벤트 시각을 확인해 주세요.")
        if age > settings.notification_event_max_age_seconds:
            raise AppError(422, "EVENT_EXPIRED", "알림 요청의 유효 시간이 지났습니다.")
        reminder = CargoReminder(
            user_id=user_id,
            plan_id=plan_id,
            idempotency_key=key,
            request_hash=fingerprint,
            **payload.model_dump(),
        )
        session.add(reminder)
        session.flush()
        devices = session.scalars(
            select(PushDevice)
            .where(PushDevice.user_id == user_id, PushDevice.active.is_(True))
            .order_by(PushDevice.device_id)
        ).all()
        if devices:
            sender.ensure_ready()
        for device in devices:
            session.add(
                PushDelivery(
                    notification_id=reminder.notification_id,
                    device_id=device.device_id,
                    token_hash=device.token_hash,
                )
            )
        return reminder.notification_id, True


def _deliver(
    session: Session,
    notification_id: UUID,
    device_id: UUID,
    settings: Settings,
    sender: PushSender,
) -> None:
    delivery_key = (notification_id, device_id)
    with session.begin():
        delivery = session.get(PushDelivery, delivery_key)
        if delivery.status == "SENDING":
            # 이전 실행이 발송 도중 종료되었다면 중복 발송 대신 확인 불가로 남깁니다.
            delivery.status = "UNKNOWN"
            delivery.error_code = "SEND_OUTCOME_UNKNOWN"
            return
        if delivery.status not in {"PREPARED", "RETRYABLE"}:
            return
        reminder = session.get(CargoReminder, notification_id)
        if (
            _now() - reminder.occurred_at
        ).total_seconds() > settings.notification_event_max_age_seconds:
            delivery.status = "FAILED"
            delivery.error_code = "EVENT_EXPIRED"
            delivery.retry_at = None
            return
        if delivery.retry_at is not None and delivery.retry_at > _now():
            return
        delivery.status = "SENDING"
        delivery.error_code = None
        delivery.retry_at = None
        delivery.attempt_count += 1

    # SENDING을 먼저 저장하여 외부 발송 후 DB 오류가 나도 재전송하지 않습니다.
    with session.begin():
        delivery = session.get(PushDelivery, delivery_key)
        reminder = session.get(CargoReminder, notification_id)
        device = session.get(PushDevice, device_id, with_for_update=True, populate_existing=True)
        if (
            device is None
            or not device.active
            or device.user_id != reminder.user_id
            or device.token_hash != delivery.token_hash
        ):
            delivery.status = "SKIPPED"
            delivery.error_code = "DEVICE_CHANGED"
            return
        token = device.token
        plan_id = reminder.plan_id

    # 네트워크 응답을 기다리는 동안 DB 연결이나 기기 갱신을 잠그지 않습니다.
    try:
        result = sender.send(token, notification_id, plan_id)
    except Exception:
        # 토큰과 외부 응답 원문은 로그에 남기지 않습니다.
        logger.error("푸시 발송 결과를 확인할 수 없습니다.")
        with session.begin():
            delivery = session.get(PushDelivery, delivery_key)
            delivery.status = "UNKNOWN"
            delivery.error_code = "SEND_OUTCOME_UNKNOWN"
        return

    with session.begin():
        delivery = session.get(PushDelivery, delivery_key)
        delivery.error_code = result.error_code
        if result.status == "INVALID_TOKEN":
            delivery.status = "FAILED"
            device = session.get(
                PushDevice, device_id, with_for_update=True, populate_existing=True
            )
            if device is not None and device.token_hash == delivery.token_hash:
                _deactivate(device)
        else:
            delivery.status = result.status
        if delivery.status == "RETRYABLE":
            delay = max(settings.push_retry_delay_seconds, result.retry_after_seconds or 0)
            delivery.retry_at = _now() + timedelta(seconds=delay)


def _reminder_data(session: Session, notification_id: UUID) -> CargoReminderData:
    with session.begin():
        reminder = session.get(CargoReminder, notification_id)
        deliveries = session.scalars(
            select(PushDelivery)
            .where(PushDelivery.notification_id == notification_id)
            .order_by(PushDelivery.device_id)
        ).all()
        accepted = sum(item.status == "ACCEPTED" for item in deliveries)
        if not deliveries:
            status = "NO_DEVICES"
        elif accepted == len(deliveries):
            status = "ACCEPTED"
        elif accepted:
            status = "PARTIAL"
        elif any(item.status == "RETRYABLE" for item in deliveries):
            status = "RETRYABLE"
        elif any(item.status == "UNKNOWN" for item in deliveries):
            status = "UNKNOWN"
        else:
            status = "FAILED"
        return CargoReminderData(
            notification_id=notification_id,
            plan_id=reminder.plan_id,
            event_type=reminder.event_type,
            status=status,
            accepted_count=accepted,
            deliveries=[
                DeliveryData.model_validate(item, from_attributes=True) for item in deliveries
            ],
        )


def send_cargo_reminder(
    session: Session,
    user_id: str,
    plan_id: UUID,
    key: UUID,
    payload: CargoReminderCreate,
    settings: Settings,
    sender: PushSender,
) -> tuple[CargoReminderData, bool]:
    with request_guard(session, user_id, "cargo-reminder", key):
        notification_id, created = _prepare_reminder(
            session, user_id, plan_id, key, payload, settings, sender
        )
        with session.begin():
            device_ids = session.scalars(
                select(PushDelivery.device_id)
                .where(PushDelivery.notification_id == notification_id)
                .order_by(PushDelivery.device_id)
            ).all()
        for device_id in device_ids:
            _deliver(session, notification_id, device_id, settings, sender)
        return _reminder_data(session, notification_id), created
