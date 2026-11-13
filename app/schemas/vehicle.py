from decimal import Decimal
from itertools import combinations
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

Coordinate = Annotated[
    Decimal,
    BeforeValidator(require_number),
    Field(decimal_places=1, allow_inf_nan=False),
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


class SpaceOrigin(VehicleSchema):
    x_cm: Coordinate
    y_cm: Coordinate
    z_cm: Coordinate


class VehicleSpace(PackingSpace):
    space_id: NonEmptyText
    kind: Literal["TRUNK", "SEAT", "FOLDED_SEAT"]
    origin_cm: SpaceOrigin
    seat_codes: tuple[SeatCode, ...]
    opening: Opening | None
    data_source: Literal["DUMMY", "MEASURED"]

    @model_validator(mode="after")
    def validate_seats(self) -> Self:
        if len(set(self.seat_codes)) != len(self.seat_codes):
            raise ValueError("공간에 같은 좌석을 중복 지정할 수 없습니다.")
        if (self.kind == "TRUNK") != (len(self.seat_codes) == 0):
            raise ValueError("트렁크는 좌석이 없어야 하고 좌석 공간은 좌석이 필요합니다.")
        return self


def spaces_overlap(first: VehicleSpace, second: VehicleSpace) -> bool:
    return all(
        first_start < second_start + second_size and second_start < first_start + first_size
        for first_start, first_size, second_start, second_size in (
            (first.origin_cm.x_cm, first.width_cm, second.origin_cm.x_cm, second.width_cm),
            (first.origin_cm.y_cm, first.depth_cm, second.origin_cm.y_cm, second.depth_cm),
            (first.origin_cm.z_cm, first.height_cm, second.origin_cm.z_cm, second.height_cm),
        )
    )


class PackingProfile(VehicleSchema):
    profile_id: NonEmptyText
    target_folded_segments: tuple[NonEmptyText, ...]
    spaces: tuple[VehicleSpace, ...] = Field(min_length=1)
    packing_space: PackingSpace | None = None

    @model_validator(mode="after")
    def validate_spaces(self) -> Self:
        if len({space.space_id for space in self.spaces}) != len(self.spaces):
            raise ValueError("같은 조합에 공간 ID를 중복 등록할 수 없습니다.")
        for first, second in combinations(self.spaces, 2):
            if set(first.seat_codes) & set(second.seat_codes):
                raise ValueError("같은 좌석의 공간을 중복 사용할 수 없습니다.")
            if spaces_overlap(first, second):
                raise ValueError("같은 조합의 적재 공간은 서로 겹칠 수 없습니다.")

        expected = None
        if len(self.spaces) == 1:
            space = self.spaces[0]
            expected = PackingSpace(
                width_cm=space.width_cm, depth_cm=space.depth_cm, height_cm=space.height_cm
            )
        if "packing_space" in self.model_fields_set and self.packing_space != expected:
            raise ValueError("packing_space는 단일 공간 치수이며 여러 공간이면 null입니다.")
        object.__setattr__(self, "packing_space", expected)
        return self


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

    @model_validator(mode="before")
    @classmethod
    def expand_legacy_profiles(cls, value: object) -> object:
        # 기존 단일 공간 차량 데이터도 같은 공간 모델로 읽습니다.
        if not isinstance(value, dict) or not isinstance(
            value.get("packing_profiles"), (list, tuple)
        ):
            return value
        profiles = []
        for profile in value["packing_profiles"]:
            if not isinstance(profile, dict) or "spaces" in profile:
                profiles.append(profile)
                continue
            dimensions = profile.get("packing_space")
            segments = profile.get("target_folded_segments")
            if not isinstance(dimensions, dict) or not isinstance(segments, (list, tuple)):
                profiles.append(profile)
                continue
            if not all(isinstance(segment, str) for segment in segments):
                profiles.append(profile)
                continue
            profiles.append(
                {
                    **profile,
                    "profile_id": profile.get("profile_id", "_".join(segments) or "TRUNK_ONLY"),
                    "spaces": [
                        {
                            **dimensions,
                            "space_id": "TRUNK",
                            "kind": "TRUNK",
                            "origin_cm": {"x_cm": 0, "y_cm": 0, "z_cm": 0},
                            "seat_codes": [],
                            "opening": value.get("opening"),
                            "data_source": value.get("data_source"),
                        }
                    ],
                }
            )
        return {**value, "packing_profiles": profiles}

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
        profile_ids: set[str] = set()
        known_spaces: dict[str, VehicleSpace] = {}
        segment_seats_by_code = {
            segment.code: set(segment.seat_codes) for segment in self.fold_segments
        }
        for profile in self.packing_profiles:
            if profile.profile_id in profile_ids:
                raise ValueError("프로필 ID를 중복 등록할 수 없습니다.")
            profile_ids.add(profile.profile_id)
            combination = frozenset(profile.target_folded_segments)
            if len(combination) != len(profile.target_folded_segments):
                raise ValueError("하나의 조합에 같은 폴딩 구간을 중복 지정할 수 없습니다.")
            if not combination <= segments:
                raise ValueError("등록되지 않은 폴딩 구간을 사용할 수 없습니다.")
            if combination in combinations:
                raise ValueError("같은 폴딩 조합을 중복 등록할 수 없습니다.")
            combinations.add(combination)
            folded_seats = set().union(*(segment_seats_by_code[code] for code in combination))
            for space in profile.spaces:
                space_seats = set(space.seat_codes)
                if not space_seats <= seats or self.driver_seat in space_seats:
                    raise ValueError("적재 공간에 미지원 좌석이나 운전석을 사용할 수 없습니다.")
                if space.kind == "SEAT" and space_seats & folded_seats:
                    raise ValueError("접은 좌석의 일반 좌석 공간을 사용할 수 없습니다.")
                if space.kind == "FOLDED_SEAT" and not space_seats <= folded_seats:
                    raise ValueError("폴딩 공간의 좌석은 해당 조합에서 접혀 있어야 합니다.")
                if space.space_id in known_spaces and known_spaces[space.space_id] != space:
                    raise ValueError("같은 공간 ID는 모든 조합에서 동일한 공간을 나타내야 합니다.")
                known_spaces[space.space_id] = space
        if frozenset() not in combinations:
            raise ValueError("접지 않은 기본 트렁크 조합이 필요합니다.")
        if self.data_source == "MEASURED" and any(
            space.data_source == "DUMMY" for space in known_spaces.values()
        ):
            raise ValueError("더미 공간이 포함된 차량은 실측 모델로 표시할 수 없습니다.")
        return self


class VehicleListData(VehicleSchema):
    vehicles: list[VehicleSummary]
