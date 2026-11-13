from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user_id
from app.db.session import get_db
from app.schemas.ai import AiMessage, AiMessageData
from app.schemas.common import ApiResponse
from app.services.ai import respond

router = APIRouter(prefix="/ai", tags=["ai"])


@router.post("/messages", response_model=ApiResponse[AiMessageData])
def ai_message(
    payload: AiMessage,
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    key: Annotated[UUID, Header(alias="Idempotency-Key")],
):
    result = respond(session, payload, user_id, key, request.app.state.settings)
    return ApiResponse(code="SUCCESS", message="대화 요청을 처리했습니다.", data=result)
