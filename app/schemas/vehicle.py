from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    model_validator,
)

SeatCode = Literal[
    "ROW_1_LEFT",
    "ROW_1_CENTER",
    "ROW_1_RIGHT",
    "ROW_2_LEFT",
    "ROW_2_CENTER",
    "ROW_2_RIGHT",
    "ROW_3_LEFT",
    "ROW_3_CENTER",
    "ROW_3_RIGHT",
]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def require_number(value: object) -> object:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("치수는 숫자로 입력해 주세요.")
    return value


Centimeters = Annotated[
    Decimal,
    BeforeValidator(require_number),
    Field(gt=0, decimal_places=1, allow_inf_nan=False),
    PlainSerializer(float, return_type=float, when_used="json"),
]


class VehicleSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PackingSpace(VehicleSchema):
    width_cm: Centimeters
    depth_cm: Centimeters
    height_cm: Centimeters


class Opening(VehicleSchema):
    width_cm: Centimeters
    height_cm: Centimeters


class FoldSegment(VehicleSchema):
    code: NonEmptyText
    seat_codes: tuple[SeatCode, ...] = Field(min_length=1)


class PackingProfile(VehicleSchema):
    target_folded_segments: tuple[NonEmptyText, ...]
    packing_space: PackingSpace


class VehicleSummary(VehicleSchema):
    vehicle_id: NonEmptyText
    name: NonEmptyText
    seat_capacity: int = Field(ge=1, le=9, strict=True)
    data_version: NonEmptyText
    data_source: Literal["DUMMY", "MEASURED"]


class VehicleDetail(VehicleSummary):
    seat_codes: tuple[SeatCode, ...] = Field(min_length=1)
    driver_seat: SeatCode
    fold_segments: tuple[FoldSegment, ...]
    packing_profiles: tuple[PackingProfile, ...] = Field(min_length=1)
    opening: Opening | None

    @model_validator(mode="after")
    def validate_seats_and_profiles(self) -> Self:
        seats = set(self.seat_codes)
        if len(seats) != len(self.seat_codes) or len(seats) != self.seat_capacity:
            raise ValueError("좌석 코드와 정원이 일치해야 하며 중복할 수 없습니다.")
        if self.driver_seat not in seats:
            raise ValueError("운전석은 지원 좌석에 포함되어야 합니다.")

        segments: set[str] = set()
        folding_seats: set[str] = set()
        for segment in self.fold_segments:
            segment_seats = set(segment.seat_codes)
            if segment.code in segments or len(segment_seats) != len(segment.seat_codes):
                raise ValueError("폴딩 구간과 구간 내 좌석은 중복할 수 없습니다.")
            if not segment_seats <= seats or self.driver_seat in segment_seats:
                raise ValueError("폴딩 구간에 미지원 좌석이나 운전석을 넣을 수 없습니다.")
            if folding_seats & segment_seats:
                raise ValueError("각 좌석은 하나의 폴딩 구간에만 속해야 합니다.")
            segments.add(segment.code)
            folding_seats.update(segment_seats)

        combinations: set[frozenset[str]] = set()
        for profile in self.packing_profiles:
            combination = frozenset(profile.target_folded_segments)
            if len(combination) != len(profile.target_folded_segments):
                raise ValueError("하나의 조합에 같은 폴딩 구간을 중복 지정할 수 없습니다.")
            if not combination <= segments:
                raise ValueError("등록되지 않은 폴딩 구간을 사용할 수 없습니다.")
            if combination in combinations:
                raise ValueError("같은 폴딩 조합을 중복 등록할 수 없습니다.")
            combinations.add(combination)
        if frozenset() not in combinations:
            raise ValueError("접지 않은 기본 트렁크 조합이 필요합니다.")
        return self


class VehicleListData(VehicleSchema):
    vehicles: list[VehicleSummary]
