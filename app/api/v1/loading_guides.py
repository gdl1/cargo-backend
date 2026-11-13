from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user_id
from app.db.session import get_db
from app.schemas.common import ApiResponse
from app.schemas.loading_guide import LoadingGuideCreate, LoadingGuideData
from app.services.loading_guides import create_loading_guide

router = APIRouter(prefix="/ai", tags=["배치 가이드"])


@router.post(
    "/loading-guides",
    response_model=ApiResponse[LoadingGuideData],
    summary="선택한 배치의 안내 문구 생성",
)
def loading_guide(
    payload: LoadingGuideCreate,
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> ApiResponse[LoadingGuideData]:
    data = create_loading_guide(session, payload, user_id, key, request.app.state.settings)
    message = "배치 안내를 준비했습니다." if data.source == "AI" else "기본 배치 안내를 제공합니다."
    return ApiResponse(code="SUCCESS", message=message, data=data)
