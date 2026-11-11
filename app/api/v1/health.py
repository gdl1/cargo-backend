import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.common import ApiResponse, ErrorResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/health", tags=["상태 확인"])


class HealthData(BaseModel):
    status: Literal["ok"] = "ok"


class DatabaseHealthData(HealthData):
    database: Literal["ok"] = "ok"


@router.get("", response_model=ApiResponse[HealthData], summary="서버 응답 확인")
def health() -> ApiResponse[HealthData]:
    return ApiResponse(code="SUCCESS", message="서버가 정상적으로 응답합니다.", data=HealthData())


@router.get(
    "/db",
    response_model=ApiResponse[DatabaseHealthData],
    responses={503: {"model": ErrorResponse, "description": "DB 연결 실패"}},
    summary="실제 DB 연결 확인",
)
def database_health(
    session: Annotated[Session, Depends(get_db)],
) -> ApiResponse[DatabaseHealthData] | JSONResponse:
    try:
        session.execute(text("SELECT 1")).scalar_one()
    except SQLAlchemyError as exc:
        logger.warning("DB 상태 확인 실패: %s", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content=ErrorResponse(
                code="DATABASE_UNAVAILABLE", message="DB에 연결할 수 없습니다."
            ).model_dump(),
        )
    return ApiResponse(
        code="SUCCESS", message="DB에 정상적으로 연결됩니다.", data=DatabaseHealthData()
    )
