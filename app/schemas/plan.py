import math
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    WithJsonSchema,
    field_validator,
)

from app.schemas.vehicle import Centimeters, NonEmptyText, SeatCode


def require_database_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("문자열에 널 문자를 포함할 수 없습니다.")
    return value


def require_serializable_dimension(value: Decimal) -> Decimal:
    numeric_value = float(value)
    if not math.isfinite(numeric_value) or Decimal(str(numeric_value)) != value:
        raise ValueError("치수는 정밀도 손실 없이 JSON 숫자로 표현할 수 있어야 합니다.")
    return value


PlanCentimeters = Annotated[
    Centimeters,
    AfterValidator(require_serializable_dimension),
    WithJsonSchema({"type": "number", "exclusiveMinimum": 0, "multipleOf": 0.1}, mode="validation"),
]


class PlanSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanItemCreate(PlanSchema):
    name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
        AfterValidator(require_database_text),
    ]
    width_cm: PlanCentimeters
    depth_cm: PlanCentimeters
    height_cm: PlanCentimeters
    quantity: int = Field(ge=1, le=2147483647, strict=True)


class PlanCreate(PlanSchema):
    vehicle_id: Annotated[NonEmptyText, AfterValidator(require_database_text)]
    passenger_count: int = Field(ge=1, le=9, strict=True)
    car_seats: list[SeatCode] = Field(default_factory=list)
    retained_seats: list[SeatCode] = Field(default_factory=list)
    items: list[PlanItemCreate] = Field(min_length=1)

    @field_validator("car_seats", "retained_seats")
    @classmethod
    def check_unique_seats(cls, value: list[SeatCode]) -> list[SeatCode]:
        if len(set(value)) != len(value):
            raise ValueError("같은 목록에 좌석을 중복 지정할 수 없습니다.")
        return value


class PlanItemData(PlanItemCreate):
    item_id: UUID


class PlanData(PlanSchema):
    plan_id: UUID
    vehicle_id: str
    passenger_count: int
    car_seats: list[SeatCode]
    retained_seats: list[SeatCode]
    items: list[PlanItemData]
    created_at: AwareDatetime
    latest_calculation_id: UUID | None = None

    @field_validator("created_at")
    @classmethod
    def utc_created_at(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class PlanSummary(PlanSchema):
    plan_id: UUID
    vehicle_id: str
    vehicle_name: str
    passenger_count: int
    item_type_count: int = Field(ge=0)
    item_total_quantity: int = Field(ge=0)
    created_at: AwareDatetime
    latest_calculation_id: UUID | None = None

    @field_validator("created_at")
    @classmethod
    def utc_created_at(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class PlanListData(PlanSchema):
    plans: list[PlanSummary]
    limit: int
    offset: int
    total: int = Field(ge=0)
