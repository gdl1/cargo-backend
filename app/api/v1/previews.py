from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user_id
from app.db.session import get_db
from app.schemas.calculation import PreviewCreate, PreviewData
from app.schemas.common import ApiResponse
from app.services.previews import create_preview

router = APIRouter(prefix="/calculations", tags=["적재 계산"])


@router.post("/preview", status_code=201, response_model=ApiResponse[PreviewData])
def preview_calculation(
    payload: PreviewCreate,
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(get_db)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
):
    data, created = create_preview(
        session, payload, user_id, idempotency_key, request.app.state.settings
    )
    response.status_code = 201 if created else 200
    message = (
        "현재 적재 상태를 확인해 주세요."
        if data.calculation.result_code == "STATE_REQUIRED"
        else "미리보기 결과를 생성했습니다."
    )
    return ApiResponse(code="SUCCESS", message=message, data=data)
