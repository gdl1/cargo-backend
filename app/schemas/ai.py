from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from app.schemas.calculation import CalculationData, VehicleState
from app.schemas.plan import PlanInputUpdate, PlanSchema
from app.schemas.products import ProductCandidateData


class AiContext(PlanSchema):
    scenario: Literal["CHECK_ITEM", "EDIT_PLAN", "ADD_TO_LOAD"]
    vehicle_id: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ]
    plan_id: UUID | None = None
    plan_revision: int | None = Field(default=None, ge=1, strict=True)
    base_calculation_id: UUID | None = None
    base_arrangement_id: UUID | None = None
    cargo_state: Literal["EMPTY", "SELECTED_ARRANGEMENT", "UNKNOWN"] = "UNKNOWN"
    vehicle_state: VehicleState | None = None

    @model_validator(mode="after")
    def require_context(self) -> Self:
        requires_plan = self.scenario != "CHECK_ITEM" or self.cargo_state == "SELECTED_ARRANGEMENT"
        if requires_plan and (self.plan_id is None or self.plan_revision is None):
            raise ValueError("현재 계획과 버전을 지정해 주세요.")
        if (self.plan_id is None) != (self.plan_revision is None):
            raise ValueError("계획 ID와 버전을 함께 지정해 주세요.")
        requires_base = self.scenario == "ADD_TO_LOAD" or self.cargo_state == "SELECTED_ARRANGEMENT"
        if requires_base and (self.base_calculation_id is None or self.base_arrangement_id is None):
            raise ValueError("선택한 계산과 배치 ID를 지정해 주세요.")
        if (self.base_calculation_id is None) != (self.base_arrangement_id is None):
            raise ValueError("계산과 배치 ID를 함께 지정해 주세요.")
        if self.base_calculation_id is not None and self.plan_id is None:
            raise ValueError("선택 배치에는 원본 계획이 필요합니다.")
        if self.scenario == "ADD_TO_LOAD" and self.cargo_state != "SELECTED_ARRANGEMENT":
            raise ValueError("짐 추가에는 현재 선택한 배치를 지정해 주세요.")
        return self


class AiAnswer(PlanSchema):
    question_id: str = Field(min_length=1, max_length=100)
    answer: Any


class AiMessage(PlanSchema):
    conversation_id: UUID | None = None
    conversation_version: int | None = Field(default=None, ge=1, strict=True)
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    context: AiContext
    answers: list[AiAnswer] = Field(default_factory=list, max_length=20)
    selected_product_candidate_id: UUID | None = None

    @model_validator(mode="after")
    def require_version(self) -> Self:
        if (self.conversation_id is None) != (self.conversation_version is None):
            raise ValueError("기존 대화에는 마지막 대화 버전을 함께 지정해 주세요.")
        if self.conversation_id is None and self.answers:
            raise ValueError("보완 답변은 기존 대화에서 발급된 질문에만 보낼 수 있습니다.")
        if "\x00" in self.message or "\x00" in self.context.vehicle_id:
            raise ValueError("문자열에 널 문자를 포함할 수 없습니다.")
        ids = [answer.question_id for answer in self.answers]
        if len(ids) != len(set(ids)):
            raise ValueError("같은 질문에 중복 답변할 수 없습니다.")
        return self


class AiQuestion(PlanSchema):
    question_id: str
    field: str
    message: str
    choices: list[str] = Field(default_factory=list)


class AiMessageData(PlanSchema):
    conversation_id: UUID
    conversation_version: int
    status: Literal["NEEDS_INPUT", "READY", "NO_RESULT"]
    reply: str
    questions: list[AiQuestion]
    product_candidates: list[ProductCandidateData]
    proposed_plan: PlanInputUpdate | None = None
    preview_id: UUID | None = None
    expires_at: AwareDatetime | None = None
    calculation: CalculationData | None = None
