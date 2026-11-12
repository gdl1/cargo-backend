import math
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal, Self
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
    model_validator,
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


Orientation = Literal["WDH", "WHD", "DWH", "DHW", "HWD", "HDW"]
ALL_ORIENTATIONS: list[Orientation] = ["WDH", "WHD", "DWH", "DHW", "HWD", "HDW"]
TransportState = Literal["PACKAGED", "ASSEMBLED", "FOLDED", "OTHER", "UNKNOWN"]
DimensionSource = Literal["AR", "MANUAL", "SAVED_ITEM", "PRODUCT_SOURCE", "UNSPECIFIED"]
PlanCategory = Literal["TRAVEL", "LEISURE", "MOVING"]
ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    AfterValidator(require_database_text),
]
OptionalText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=2000),
    AfterValidator(require_database_text),
]


class PlanMetadata(PlanSchema):
    name: ShortText | None = None
    category: PlanCategory | None = None
    memo: OptionalText | None = None
    is_favorite: bool = Field(default=False, strict=True)


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
    required_quantity: int = Field(default=0, ge=0, le=2147483647, strict=True)
    allowed_orientations: list[Orientation] = Field(
        default_factory=lambda: ALL_ORIENTATIONS.copy(), min_length=1
    )
    transport_state: TransportState = "UNKNOWN"
    dimension_source: DimensionSource = "UNSPECIFIED"
    material: ShortText | None = None
    saved_item_id: UUID | None = None

    @model_validator(mode="after")
    def check_item_conditions(self) -> Self:
        if self.required_quantity > self.quantity:
            raise ValueError("필수 수량은 전체 수량보다 클 수 없습니다.")
        if len(set(self.allowed_orientations)) != len(self.allowed_orientations):
            raise ValueError("허용 회전을 중복 지정할 수 없습니다.")
        return self


class PlanCreate(PlanMetadata):
    vehicle_id: Annotated[NonEmptyText, AfterValidator(require_database_text)]
    passenger_count: int = Field(ge=1, le=9, strict=True)
    passenger_seats: list[SeatCode] | None = None
    car_seats: list[SeatCode] = Field(default_factory=list)
    retained_seats: list[SeatCode] = Field(default_factory=list)
    items: list[PlanItemCreate] = Field(min_length=1)

    @field_validator("car_seats", "retained_seats", "passenger_seats")
    @classmethod
    def check_unique_seats(cls, value: list[SeatCode] | None) -> list[SeatCode] | None:
        if value is None:
            return value
        if len(set(value)) != len(value):
            raise ValueError("같은 목록에 좌석을 중복 지정할 수 없습니다.")
        return value


class PlanItemData(PlanItemCreate):
    item_id: UUID


class PlanItemUpdate(PlanItemCreate):
    item_id: UUID | None = None


class PlanInputUpdate(PlanCreate):
    items: list[PlanItemUpdate] = Field(min_length=1)

    @field_validator("items")
    @classmethod
    def check_unique_item_ids(cls, value: list[PlanItemUpdate]) -> list[PlanItemUpdate]:
        ids = [item.item_id for item in value if item.item_id is not None]
        if len(ids) != len(set(ids)):
            raise ValueError("동일한 짐 ID를 중복 지정할 수 없습니다.")
        return value


class PlanFromResult(PlanSchema):
    source_preview_id: UUID | None = None
    source_calculation_id: UUID | None = None
    selected_arrangement_id: UUID

    @model_validator(mode="after")
    def check_source(self) -> Self:
        if (self.source_preview_id is None) == (self.source_calculation_id is None):
            raise ValueError("미리보기 또는 저장된 계산 중 하나만 지정해 주세요.")
        return self


class PlanUpdate(PlanSchema):
    expected_revision: int = Field(ge=1, strict=True)
    plan: PlanInputUpdate | None = None
    source_preview_id: UUID | None = None
    source_calculation_id: UUID | None = None
    selected_arrangement_id: UUID | None = None

    @model_validator(mode="after")
    def check_source(self) -> Self:
        count = sum(
            value is not None
            for value in (self.plan, self.source_preview_id, self.source_calculation_id)
        )
        if count != 1 or (self.plan is None) != (self.selected_arrangement_id is not None):
            raise ValueError("계획 입력 또는 선택 결과 중 한 방식만 사용해 주세요.")
        return self


class PlanMetadataUpdate(PlanSchema):
    expected_revision: int = Field(ge=1, strict=True)
    name: ShortText | None = None
    category: PlanCategory | None = None
    memo: OptionalText | None = None
    is_favorite: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def require_change(self) -> Self:
        if not (self.model_fields_set - {"expected_revision"}):
            raise ValueError("변경할 항목을 지정해 주세요.")
        return self


class PlanSelection(PlanSchema):
    expected_revision: int = Field(ge=1, strict=True)
    calculation_id: UUID
    arrangement_id: UUID


class PlanSelectionData(PlanSchema):
    plan_id: UUID
    revision: int
    selected_calculation_id: UUID
    selected_arrangement_id: UUID


class PlanData(PlanMetadata):
    plan_id: UUID
    vehicle_id: str
    passenger_count: int
    passenger_seats: list[SeatCode] | None = None
    car_seats: list[SeatCode]
    retained_seats: list[SeatCode]
    items: list[PlanItemData]
    created_at: AwareDatetime
    latest_calculation_id: UUID | None = None
    revision: int = 1
    updated_at: AwareDatetime | None = None
    selected_calculation_id: UUID | None = None
    selected_arrangement_id: UUID | None = None
    favorited_at: AwareDatetime | None = None

    @field_validator("created_at")
    @classmethod
    def utc_created_at(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class PlanItemSummary(PlanSchema):
    item_id: UUID
    name: str
    quantity: int


class PlanSummary(PlanMetadata):
    plan_id: UUID
    vehicle_id: str
    vehicle_name: str
    passenger_count: int
    item_type_count: int = Field(ge=0)
    item_total_quantity: int = Field(ge=0)
    created_at: AwareDatetime
    latest_calculation_id: UUID | None = None
    revision: int = 1
    updated_at: AwareDatetime | None = None
    selected_calculation_id: UUID | None = None
    selected_arrangement_id: UUID | None = None
    favorited_at: AwareDatetime | None = None
    item_summaries: list[PlanItemSummary] = Field(default_factory=list)

    @field_validator("created_at")
    @classmethod
    def utc_created_at(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class PlanListData(PlanSchema):
    plans: list[PlanSummary]
    limit: int
    offset: int
    total: int = Field(ge=0)
