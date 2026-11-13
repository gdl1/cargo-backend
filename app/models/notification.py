from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PushDevice(Base):
    __tablename__ = "push_device"
    __table_args__ = (
        Index("ix_push_device_user_active", "user_id", "active"),
        UniqueConstraint("token_hash", name="uq_push_device_token_hash"),
        CheckConstraint("btrim(user_id) <> ''", name="user_not_blank"),
        CheckConstraint("platform = 'ANDROID'", name="platform_valid"),
        CheckConstraint(
            "(active AND token IS NOT NULL AND token_hash IS NOT NULL) OR "
            "(NOT active AND token IS NULL AND token_hash IS NULL)",
            name="token_state_valid",
        ),
    )

    device_id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(Text)
    token: Mapped[str | None] = mapped_column(Text)
    token_hash: Mapped[str | None] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CargoReminder(Base):
    __tablename__ = "cargo_reminder"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_cargo_reminder_user_key"),
        CheckConstraint("btrim(user_id) <> ''", name="user_not_blank"),
        CheckConstraint(
            "event_type IN ('DESTINATION_ARRIVED', 'DOOR_OPENED')", name="event_type_valid"
        ),
    )

    notification_id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[str] = mapped_column(Text)
    plan_id: Mapped[UUID] = mapped_column(ForeignKey("plan.plan_id", ondelete="CASCADE"))
    idempotency_key: Mapped[UUID]
    request_hash: Mapped[str] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PushDelivery(Base):
    __tablename__ = "push_delivery"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PREPARED', 'SENDING', 'ACCEPTED', 'RETRYABLE', 'FAILED', 'UNKNOWN', "
            "'SKIPPED')",
            name="status_valid",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_valid"),
    )

    notification_id: Mapped[UUID] = mapped_column(
        ForeignKey("cargo_reminder.notification_id", ondelete="CASCADE"), primary_key=True
    )
    device_id: Mapped[UUID] = mapped_column(
        ForeignKey("push_device.device_id", ondelete="RESTRICT"), primary_key=True
    )
    token_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="PREPARED", server_default="PREPARED")
    error_code: Mapped[str | None] = mapped_column(Text)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(default=0, server_default="0")
