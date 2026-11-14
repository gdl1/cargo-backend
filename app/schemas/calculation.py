from typing import Annotated, Literal, NotRequired, Self, TypedDict
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.plan import PlanInputUpdate
from app.schemas.vehicle import Opening, SeatCode


class CalculationSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SeatObservation(CalculationSchema):
    seat_code: SeatCode
    occupancy: Literal["OCCUPIED", "EMPTY", "UNKNOWN"]
    source: Literal["SDK", "USER", "UNKNOWN"]


class FoldObservation(CalculationSchema):
    segment_code: str = Field(min_length=1, max_length=100)
    state: Literal["FOLDED", "UNFOLDED", "UNKNOWN"]
    source: Literal["SDK", "USER", "SELECTED_ARRANGEMENT", "UNKNOWN"]


class VehicleState(CalculationSchema):
    observed_at: AwareDatetime
    seats: list[SeatObservation] = Field(default_factory=list)
    fold_segments: list[FoldObservation] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_observations(self) -> Self:
        if len({seat.seat_code for seat in self.seats}) != len(self.seats):
            raise ValueError("좌석 관측을 중복 지정할 수 없습니다.")
        if len({segment.segment_code for segment in self.fold_segments}) != len(self.fold_segments):
            raise ValueError("폴딩 관측을 중복 지정할 수 없습니다.")
        return self


class CalculationCreate(CalculationSchema):
    current_folded_segments: list[str] | None = None
    occupied_seats: list[SeatCode] | None = None
    vehicle_state: VehicleState | None = None
    expected_revision: int | None = Field(default=None, ge=1, strict=True)
    packing_mode: Literal["REPACK_ALL", "KEEP_EXISTING"] = "REPACK_ALL"
    base_calculation_id: UUID | None = None
    base_arrangement_id: UUID | None = None
    allow_exclusions: bool = Field(default=False, strict=True)
    cargo_state: Literal["EMPTY", "SELECTED_ARRANGEMENT", "UNKNOWN"] | None = None

    @field_validator("current_folded_segments", "occupied_seats")
    @classmethod
    def unique_codes(cls, value):
        if value is not None and (len(set(value)) != len(value) or any(not code for code in value)):
            raise ValueError("상태 코드는 비어 있거나 중복될 수 없습니다.")
        return value

    @model_validator(mode="after")
    def require_base(self) -> Self:
        if (self.base_calculation_id is None) != (self.base_arrangement_id is None):
            raise ValueError("기준 계산과 배치 ID를 함께 지정해 주세요.")
        if self.packing_mode == "KEEP_EXISTING":
            if self.cargo_state != "SELECTED_ARRANGEMENT" or self.base_calculation_id is None:
                raise ValueError("기존 배치 유지에는 선택한 배치와 현재 적재 상태가 필요합니다.")
        if self.cargo_state == "SELECTED_ARRANGEMENT" and self.base_calculation_id is None:
            raise ValueError("선택한 기준 배치를 지정해 주세요.")
        return self


ResponseId = Annotated[str, Field(json_schema_extra={"format": "uuid"})]
NonnegativeCm = Annotated[float, Field(ge=0, allow_inf_nan=False)]
PositiveInt = Annotated[int, Field(ge=1)]


class SpaceDimensions(TypedDict):
    width_cm: Annotated[float, Field(gt=0)]
    depth_cm: Annotated[float, Field(gt=0)]
    height_cm: Annotated[float, Field(gt=0)]


class SpaceOrigin(TypedDict):
    x_cm: float
    y_cm: float
    z_cm: float


class SpaceSnapshot(SpaceDimensions):
    space_id: str
    kind: Literal["TRUNK", "SEAT", "FOLDED_SEAT"]
    origin_cm: SpaceOrigin
    seat_codes: list[SeatCode]
    opening: NotRequired[Opening | None]
    connected_to_space_id: NotRequired[str]


class PlacementData(TypedDict):
    placement_id: ResponseId
    item_id: ResponseId
    unit_index: PositiveInt
    space_id: str
    x_cm: NonnegativeCm
    y_cm: NonnegativeCm
    z_cm: NonnegativeCm
    orientation: Literal["WDH", "WHD", "DWH", "DHW", "HWD", "HDW"]
    load_order: PositiveInt
    supporting_placement_ids: list[ResponseId]
    spanning_space_id: NotRequired[str]
    source_item_id: NotRequired[ResponseId]
    source_unit_index: NotRequired[PositiveInt]


class StackingAssessmentData(TypedDict):
    lower_item_id: ResponseId
    upper_item_id: ResponseId
    allowed: bool | None
    basis: Literal["LLM_ESTIMATE"]
    reason: str


class ExclusionData(TypedDict):
    item_id: ResponseId
    name: str
    quantity: PositiveInt
    unit_indices: list[PositiveInt]
    reason_code: str


class ArrangementData(TypedDict):
    arrangement_id: ResponseId
    rank: PositiveInt
    passenger_seats: list[SeatCode]
    target_folded_segments: list[str]
    seat_change_count: Annotated[int, Field(ge=0)] | None
    packing_space: SpaceDimensions | None
    profile_id: str
    packing_spaces: list[SpaceSnapshot]
    score: float | None
    placements: list[PlacementData]
    all_in_trunk: bool
    excluded_total_quantity: Annotated[int, Field(ge=0)]
    reason_codes: list[str]
    stacking_assessments: list[StackingAssessmentData]
    exclusions: NotRequired[list[ExclusionData]]


class RequiredInput(TypedDict):
    field: str
    reason: str
    seat_code: NotRequired[SeatCode]
    segment_code: NotRequired[str]


class ResultProvenance(TypedDict):
    source_calculation_id: ResponseId | None
    source_preview_id: ResponseId | None
    source_arrangement_id: ResponseId
    source_packing_mode: Literal["REPACK_ALL", "KEEP_EXISTING"]
    source_base_calculation_id: ResponseId | None
    source_base_arrangement_id: ResponseId | None


class CalculationData(CalculationSchema):
    calculation_id: UUID | None
    plan_id: UUID | None
    plan_revision: int | None
    is_success: bool
    result_code: Literal[
        "LAYOUT_FOUND",
        "EXCLUSION_OPTIONS_FOUND",
        "NO_LAYOUT_FOUND",
        "TIME_LIMIT",
        "NO_VALID_SEAT_CONFIGURATION",
        "STATE_REQUIRED",
    ]
    search_complete: bool
    packing_mode: Literal["REPACK_ALL", "KEEP_EXISTING"]
    base_calculation_id: UUID | None = None
    base_arrangement_id: UUID | None = None
    current_folded_segments: list[str] | None = None
    occupied_seats: list[SeatCode] | None = None
    vehicle_data_version: str
    input_snapshot: dict
    provenance: ResultProvenance | None = None
    arrangements: list[ArrangementData] = Field(default_factory=list)
    alternatives: list[ArrangementData] = Field(default_factory=list)
    required_inputs: list[RequiredInput] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    created_at: AwareDatetime


class PreviewCreate(CalculationSchema):
    source_plan_id: UUID | None = None
    expected_revision: int | None = Field(default=None, ge=1, strict=True)
    plan: PlanInputUpdate
    calculation_options: CalculationCreate = Field(default_factory=CalculationCreate)

    @model_validator(mode="after")
    def require_revision(self) -> Self:
        if (self.source_plan_id is None) != (self.expected_revision is None):
            raise ValueError("원본 계획과 현재 버전을 함께 지정해 주세요.")
        if self.calculation_options.packing_mode == "KEEP_EXISTING" and self.source_plan_id is None:
            raise ValueError("기존 배치 유지에는 원본 계획이 필요합니다.")
        nested = self.calculation_options.expected_revision
        if nested is not None and nested != self.expected_revision:
            raise ValueError("계획 버전 입력이 일치해야 합니다.")
        return self


class PreviewData(CalculationSchema):
    preview_id: UUID
    expires_at: AwareDatetime
    source_plan_id: UUID | None
    calculation: CalculationData
