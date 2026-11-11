import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BeforeValidator
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user_id
from app.db.session import get_db
from app.schemas.common import ApiResponse, ErrorResponse
from app.schemas.plan import PlanCreate, PlanData, PlanListData
from app.services.plans import create_plan, get_plan, list_plans

router = APIRouter(prefix="/plans", tags=["적재 계획"])


def require_query_integer(value: object) -> object:
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value) is None:
        raise ValueError("정수로 입력해 주세요.")
    return value


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


@router.get(
    "",
    response_model=ApiResponse[PlanListData],
    summary="적재 계획 목록 조회",
    responses={
        422: {"model": ErrorResponse, "description": "조회 범위 오류"},
        500: {"model": ErrorResponse, "description": "서버 오류"},
    },
)
def read_plans(
    session: Annotated[Session, Depends(get_db)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    limit: Annotated[int, Query(ge=1, le=100), BeforeValidator(require_query_integer)] = 20,
    offset: Annotated[
        int, Query(ge=0, le=9223372036854775807), BeforeValidator(require_query_integer)
    ] = 0,
) -> ApiResponse[PlanListData]:
    data = list_plans(session, user_id, limit, offset)
    return ApiResponse(code="SUCCESS", message="적재 계획 목록을 조회했습니다.", data=data)


@router.get(
    "/{plan_id}",
    response_model=ApiResponse[PlanData],
    summary="적재 계획 상세 조회",
    responses={
        404: {"model": ErrorResponse, "description": "계획 없음"},
        422: {"model": ErrorResponse, "description": "계획 ID 형식 오류"},
        500: {"model": ErrorResponse, "description": "서버 오류"},
    },
)
def read_plan(
    plan_id: UUID,
    session: Annotated[Session, Depends(get_db)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> ApiResponse[PlanData]:
    data = get_plan(session, user_id, plan_id)
    return ApiResponse(code="SUCCESS", message="적재 계획을 조회했습니다.", data=data)
