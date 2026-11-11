from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user_id
from app.db.session import get_db
from app.schemas.common import ApiResponse, ErrorResponse
from app.schemas.plan import PlanCreate, PlanData
from app.services.plans import create_plan

router = APIRouter(prefix="/plans", tags=["적재 계획"])


@router.post(
    "",
    status_code=201,
    response_model=ApiResponse[PlanData],
    summary="적재 계획 저장",
    responses={
        200: {"model": ApiResponse[PlanData], "description": "같은 요청의 재시도"},
        404: {"model": ErrorResponse, "description": "차량 없음"},
        409: {"model": ErrorResponse, "description": "요청 키 충돌 또는 처리 중"},
        422: {"model": ErrorResponse, "description": "입력값 오류"},
        500: {"model": ErrorResponse, "description": "서버 오류"},
    },
)
def save_plan(
    payload: PlanCreate,
    response: Response,
    session: Annotated[Session, Depends(get_db)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> ApiResponse[PlanData]:
    data, created = create_plan(session, payload, user_id, idempotency_key)
    response.status_code = 201 if created else 200
    response.headers["Location"] = f"/api/v1/plans/{data.plan_id}"
    return ApiResponse(code="SUCCESS", message="적재 계획을 저장했습니다.", data=data)
