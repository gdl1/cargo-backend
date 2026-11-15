from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BeforeValidator
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.dependencies import get_current_user_id
from app.api.v1.plans import require_query_integer
from app.core.exceptions import AppError
from app.db.session import get_db
from app.schemas.common import ApiResponse, ErrorResponse
from app.schemas.item import ItemCreate, ItemData, ItemFavoriteUpdate, ItemListData, ItemPhotoData
from app.services.item_photos import ITEM_PHOTO_FORMATS, ITEM_PHOTO_MAX_BYTES, photo_too_large
from app.services.items import (
    create_item,
    list_items,
    read_item_photo,
    save_item_photo,
    update_item_favorite,
)

router = APIRouter(prefix="/items", tags=["내 짐"])
PHOTO_MEDIA_SCHEMA = {
    media_type: {"schema": {"type": "string", "format": "binary"}}
    for media_type in ITEM_PHOTO_FORMATS
}


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


@router.put(
    "/{saved_item_id}/photo",
    response_model=ApiResponse[ItemPhotoData],
    summary="내 짐 사진 업로드 및 교체",
    description="JPEG 또는 PNG 파일 바이트를 보냅니다. 최대 5 MiB, 2천만 픽셀입니다.",
    openapi_extra={"requestBody": {"required": True, "content": PHOTO_MEDIA_SCHEMA}},
    responses={status: {"model": ErrorResponse} for status in (404, 409, 413, 415, 422, 503)},
)
async def upload_item_photo(
    saved_item_id: UUID,
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> ApiResponse[ItemPhotoData]:
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type not in ITEM_PHOTO_FORMATS:
        raise AppError(
            415, "ITEM_PHOTO_UNSUPPORTED_TYPE", "JPEG 또는 PNG 형식만 사용할 수 있습니다."
        )
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > ITEM_PHOTO_MAX_BYTES:
            raise photo_too_large()
        content.extend(chunk)
    data = await run_in_threadpool(
        save_item_photo,
        session,
        user_id,
        saved_item_id,
        idempotency_key,
        storage=request.app.state.item_photo_storage,
        content=bytes(content),
        content_type=content_type,
    )
    return ApiResponse(code="SUCCESS", message="내 짐 사진을 저장했습니다.", data=data)


@router.get(
    "/{saved_item_id}/photo",
    response_class=Response,
    summary="내 짐 사진 조회",
    responses={
        200: {"content": PHOTO_MEDIA_SCHEMA},
        404: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
def download_item_photo(
    saved_item_id: UUID,
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> Response:
    content, content_type = read_item_photo(
        session, user_id, saved_item_id, request.app.state.item_photo_storage
    )
    return Response(
        content,
        media_type=content_type,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )
