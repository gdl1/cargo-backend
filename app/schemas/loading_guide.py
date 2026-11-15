from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.packing import MAX_UNITS


class LoadingGuideCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: UUID | None = None
    calculation_id: UUID | None = None
    preview_id: UUID | None = None
    arrangement_id: UUID

    @model_validator(mode="after")
    def one_source(self) -> Self:
        if self.preview_id is not None:
            if self.plan_id is not None or self.calculation_id is not None:
                raise ValueError("저장된 계산과 미리보기 중 하나만 지정해 주세요.")
        elif self.plan_id is None or self.calculation_id is None:
            raise ValueError("계획과 계산 ID 또는 미리보기 ID를 지정해 주세요.")
        return self


class LoadingGuideStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: int = Field(ge=1, strict=True)
    placement_id: UUID
    text: str = Field(min_length=1, max_length=1500)


class LoadingGuideData(BaseModel):
    plan_id: UUID | None
    calculation_id: UUID | None
    preview_id: UUID | None
    arrangement_id: UUID
    source: Literal["AI", "FALLBACK"]
    reply: str
    steps: list[LoadingGuideStep] = Field(max_length=MAX_UNITS)


class GeneratedGuide(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply: str = Field(min_length=1, max_length=500)
