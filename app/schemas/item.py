from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator

from app.schemas.plan import (
    ALL_ORIENTATIONS,
    Orientation,
    PlanCentimeters,
    PlanSchema,
    ShortText,
    TransportState,
)


class ItemCreate(PlanSchema):
    name: ShortText
    width_cm: PlanCentimeters
    depth_cm: PlanCentimeters
    height_cm: PlanCentimeters
    default_quantity: int = Field(default=1, ge=1, le=2147483647, strict=True)
    allowed_orientations: list[Orientation] = Field(
        default_factory=lambda: ALL_ORIENTATIONS.copy(), min_length=1
    )
    transport_state: TransportState = "UNKNOWN"
    dimension_source: Literal["AR", "MANUAL", "PRODUCT_SOURCE", "UNSPECIFIED"] = "UNSPECIFIED"
    material: ShortText | None = None
    is_favorite: bool = Field(default=False, strict=True)

    @field_validator("allowed_orientations")
    @classmethod
    def unique_orientations(cls, value: list[Orientation]) -> list[Orientation]:
        if len(set(value)) != len(value):
            raise ValueError("허용 회전을 중복 지정할 수 없습니다.")
        return value


class ItemData(ItemCreate):
    saved_item_id: UUID
    created_at: AwareDatetime
    photo_url: str | None = Field(default=None, description="저장한 짐 사진을 여는 API 경로")


class ItemPhotoData(PlanSchema):
    saved_item_id: UUID
    photo_url: str


class ItemListData(PlanSchema):
    items: list[ItemData]
    limit: int
    offset: int
    total: int = Field(ge=0)


class ItemFavoriteUpdate(PlanSchema):
    is_favorite: bool = Field(strict=True)
