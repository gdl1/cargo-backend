from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user_id
from app.db.session import get_db
from app.schemas.calculation import CalculationCreate, CalculationData
from app.schemas.common import ApiResponse
from app.services.calculations import create_calculation, get_calculation

router = APIRouter(prefix="/plans/{plan_id}/calculations", tags=["적재 계산"])


@router.post("", status_code=201, response_model=ApiResponse[CalculationData])
def calculate_plan(
    payload: CalculationCreate,
    plan_id: UUID,
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(get_db)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
):
    data, created = create_calculation(
        session, payload, user_id, plan_id, idempotency_key, request.app.state.settings
    )
    response.status_code = 201 if created else 200
    response.headers["Location"] = f"/api/v1/plans/{plan_id}/calculations/{data.calculation_id}"
    return ApiResponse(code="SUCCESS", message="계산 결과를 저장했습니다.", data=data)


@router.get("/{calculation_id}", response_model=ApiResponse[CalculationData])
def read_calculation(
    plan_id: UUID,
    calculation_id: UUID,
    session: Annotated[Session, Depends(get_db)],
    user_id: Annotated[str, Depends(get_current_user_id)],
):
    data = get_calculation(session, user_id, plan_id, calculation_id)
    return ApiResponse(code="SUCCESS", message="계산 결과를 조회했습니다.", data=data)
