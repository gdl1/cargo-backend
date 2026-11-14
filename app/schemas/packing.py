from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.vehicle import (
    Centimeters,
    Opening,
    PositionedSpace,
    SpaceOrigin,
    validate_trunk_connections,
)

Orientation = Literal["WDH", "WHD", "DWH", "DHW", "HWD", "HDW"]
ORIENTATIONS: tuple[Orientation, ...] = ("WDH", "WHD", "DWH", "DHW", "HWD", "HDW")
MAX_UNITS = 64
Dimension = Annotated[Centimeters, Field(le=1_000_000)]


class PackingSchema(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class CargoOpening(Opening):
    model_config = PackingSchema.model_config

    width_cm: Dimension
    height_cm: Dimension


class CargoSpace(PositionedSpace):
    model_config = PackingSchema.model_config

    kind: Literal["TRUNK", "SEAT", "FOLDED_SEAT"] = "TRUNK"
    width_cm: Dimension
    depth_cm: Dimension
    height_cm: Dimension
    origin_cm: SpaceOrigin = SpaceOrigin(x_cm=0, y_cm=0, z_cm=0)
    opening: CargoOpening | None = None


class PackingItem(PackingSchema):
    item_id: str = Field(min_length=1)
    width_cm: Dimension
    depth_cm: Dimension
    height_cm: Dimension
    quantity: int = Field(ge=1, le=MAX_UNITS, strict=True)
    required_quantity: int = Field(default=0, ge=0, strict=True)
    allowed_orientations: tuple[Orientation, ...] = ORIENTATIONS

    @model_validator(mode="after")
    def validate_item(self) -> Self:
        if self.required_quantity > self.quantity:
            raise ValueError("필수 수량은 전체 수량을 넘을 수 없습니다.")
        if not self.allowed_orientations or len(set(self.allowed_orientations)) != len(
            self.allowed_orientations
        ):
            raise ValueError("허용 회전은 한 개 이상이며 중복할 수 없습니다.")
        return self


class PackingProblem(PackingSchema):
    spaces: tuple[CargoSpace, ...] = Field(min_length=1)
    items: tuple[PackingItem, ...] = Field(min_length=1, max_length=MAX_UNITS)

    @model_validator(mode="after")
    def validate_problem(self) -> Self:
        if len({item.item_id for item in self.items}) != len(self.items):
            raise ValueError("짐 ID는 중복할 수 없습니다.")
        if len({space.space_id for space in self.spaces}) != len(self.spaces):
            raise ValueError("공간 ID는 중복할 수 없습니다.")
        validate_trunk_connections(self.spaces)
        if sum(item.quantity for item in self.items) > MAX_UNITS:
            raise ValueError(f"계산할 짐은 최대 {MAX_UNITS}개입니다.")
        return self


class PackingResult(PackingSchema):
    result_code: Literal["LAYOUT_FOUND", "NO_LAYOUT_FOUND", "TIME_LIMIT"]
    search_complete: bool
    placements: list[dict] = Field(default_factory=list)
    evaluated_candidates: int = 0


def centimeters(value: int) -> float:
    return float(Decimal(value) / 10)
