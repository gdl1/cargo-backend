from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from app.schemas.plan import PlanCentimeters, require_database_text

TransportState = Literal["PACKAGED", "ASSEMBLED", "FOLDED"]
ProductText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=300),
    AfterValidator(require_database_text),
]


class ProductSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProductSearch(ProductSchema):
    query: ProductText
    transport_states: list[TransportState] = Field(
        default_factory=lambda: ["PACKAGED", "ASSEMBLED", "FOLDED"], min_length=1, max_length=3
    )

    @field_validator("query")
    @classmethod
    def no_null_character(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("검색어에 널 문자를 포함할 수 없습니다.")
        return value

    @field_validator("transport_states")
    @classmethod
    def unique_states(cls, value: list[TransportState]) -> list[TransportState]:
        if len(value) != len(set(value)):
            raise ValueError("운반 상태는 중복 지정할 수 없습니다.")
        return value


class ProductSource(ProductSchema):
    url: str
    title: ProductText


class ProductCandidateData(ProductSchema):
    candidate_id: UUID
    name: ProductText
    model: ProductText | None = None
    variant: ProductText | None = None
    transport_state: TransportState
    width_cm: PlanCentimeters | None = None
    depth_cm: PlanCentimeters | None = None
    height_cm: PlanCentimeters | None = None
    dimension_source: Literal["PRODUCT_SOURCE"] = "PRODUCT_SOURCE"
    sources: list[ProductSource]
    image_url: str | None = Field(default=None, description="제품 카드용 대표 사진의 HTTPS 주소")
    image_source_url: str | None = Field(default=None, description="사진을 확인한 제품 상세 페이지")
    requires_confirmation: Literal[True] = True


class ProductSearchData(ProductSchema):
    candidates: list[ProductCandidateData]
