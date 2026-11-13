from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BeforeValidator
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user_id
from app.api.v1.plans import require_query_integer
from app.db.session import get_db
from app.schemas.common import ApiResponse
from app.schemas.item import ItemCreate, ItemData, ItemFavoriteUpdate, ItemListData
from app.services.items import create_item, list_items, update_item_favorite

router = APIRouter(prefix="/items", tags=["내 짐"])


@router.post("", status_code=201, response_model=ApiResponse[ItemData], summary="내 짐 등록")
def save_item(
    payload: ItemCreate,
    response: Response,
    session: Annotated[Session, Depends(get_db)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> ApiResponse[ItemData]:
    data, created = create_item(session, payload, user_id, idempotency_key)
    response.status_code = 201 if created else 200
    return ApiResponse(code="SUCCESS", message="내 짐을 저장했습니다.", data=data)


@router.get("", response_model=ApiResponse[ItemListData], summary="내 짐 조회")
def read_items(
    session: Annotated[Session, Depends(get_db)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    limit: Annotated[int, Query(ge=1, le=100), BeforeValidator(require_query_integer)] = 20,
    offset: Annotated[
        int, Query(ge=0, le=9223372036854775807), BeforeValidator(require_query_integer)
    ] = 0,
    favorite: bool | None = None,
) -> ApiResponse[ItemListData]:
    return ApiResponse(
        code="SUCCESS",
        message="내 짐을 조회했습니다.",
        data=list_items(session, user_id, limit, offset, favorite),
    )


@router.patch(
    "/{saved_item_id}/favorite",
    response_model=ApiResponse[ItemData],
    summary="내 짐 즐겨찾기 변경",
)
def change_item_favorite(
    saved_item_id: UUID,
    payload: ItemFavoriteUpdate,
    session: Annotated[Session, Depends(get_db)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> ApiResponse[ItemData]:
    data = update_item_favorite(session, user_id, saved_item_id, payload, idempotency_key)
    return ApiResponse(code="SUCCESS", message="짐 즐겨찾기를 변경했습니다.", data=data)
