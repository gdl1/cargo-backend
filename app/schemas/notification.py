from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator


class DeviceRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: Annotated[str, StringConstraints(min_length=1, max_length=4096, pattern=r"^\S+$")]
    platform: Literal["ANDROID"] = "ANDROID"


class DeviceData(BaseModel):
    device_id: UUID
    platform: Literal["ANDROID"]
    active: bool
    updated_at: AwareDatetime

    @field_validator("updated_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class CargoReminderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: Literal["DESTINATION_ARRIVED", "DOOR_OPENED"]
    occurred_at: AwareDatetime

    @field_validator("occurred_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class DeliveryData(BaseModel):
    device_id: UUID
    status: Literal["ACCEPTED", "RETRYABLE", "FAILED", "UNKNOWN", "SKIPPED"]
    error_code: str | None = None
    retry_at: AwareDatetime | None = None

    @field_validator("retry_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None


class CargoReminderData(BaseModel):
    notification_id: UUID
    plan_id: UUID
    event_type: Literal["DESTINATION_ARRIVED", "DOOR_OPENED"]
    status: Literal["ACCEPTED", "PARTIAL", "RETRYABLE", "FAILED", "UNKNOWN", "NO_DEVICES"]
    accepted_count: int = Field(ge=0)
    deliveries: list[DeliveryData]
