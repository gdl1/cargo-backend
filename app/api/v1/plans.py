import re
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import AfterValidator, BeforeValidator
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user_id
from app.db.session import get_db
from app.schemas.common import ApiResponse, ErrorResponse
from app.schemas.plan import (
    PlanCategory,
    PlanCreate,
    PlanData,
    PlanFromResult,
    PlanListData,
    PlanMetadataUpdate,
    PlanSelection,
    PlanSelectionData,
    PlanUpdate,
    require_database_text,
)
from app.services.plans import (
    create_plan,
    get_plan,
    list_plans,
    select_arrangement,
    update_metadata,
    update_plan,
)

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
    payload: PlanCreate | PlanFromResult,
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
    sort: Literal["latest", "favorite"] = "latest",
    q: Annotated[str, Query(max_length=100), AfterValidator(require_database_text)] | None = None,
    category: PlanCategory | None = None,
    favorite: bool | None = None,
) -> ApiResponse[PlanListData]:
    data = list_plans(
        session, user_id, limit, offset, sort=sort, q=q, category=category, favorite=favorite
    )
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


@router.put("/{plan_id}", response_model=ApiResponse[PlanData], summary="적재 계획 변경")
def change_plan(
    plan_id: UUID,
    payload: PlanUpdate,
    session: Annotated[Session, Depends(get_db)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> ApiResponse[PlanData]:
    data = update_plan(session, payload, user_id, plan_id, idempotency_key)
    return ApiResponse(code="SUCCESS", message="적재 계획을 변경했습니다.", data=data)


@router.patch(
    "/{plan_id}/metadata",
    response_model=ApiResponse[PlanData],
    summary="계획 이름·분류·메모·즐겨찾기 변경",
)
def change_metadata(
    plan_id: UUID,
    payload: PlanMetadataUpdate,
    session: Annotated[Session, Depends(get_db)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> ApiResponse[PlanData]:
    data = update_metadata(session, payload, user_id, plan_id, idempotency_key)
    return ApiResponse(code="SUCCESS", message="계획 정보를 변경했습니다.", data=data)


@router.put(
    "/{plan_id}/selection",
    response_model=ApiResponse[PlanSelectionData],
    summary="사용할 배치 선택",
)
def choose_arrangement(
    plan_id: UUID,
    payload: PlanSelection,
    session: Annotated[Session, Depends(get_db)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> ApiResponse[PlanSelectionData]:
    data = select_arrangement(session, payload, user_id, plan_id, idempotency_key)
    return ApiResponse(code="SUCCESS", message="사용할 배치안을 선택했습니다.", data=data)
