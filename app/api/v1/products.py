from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user_id
from app.db.session import get_db
from app.schemas.common import ApiResponse
from app.schemas.products import ProductSearch, ProductSearchData
from app.services.products import search_products

router = APIRouter(prefix="/products", tags=["products"])


@router.post("/search", response_model=ApiResponse[ProductSearchData])
def product_search(
    payload: ProductSearch,
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    key: Annotated[UUID, Header(alias="Idempotency-Key")],
):
    result = search_products(session, payload, user_id, key, request.app.state.settings)
    return ApiResponse(code="SUCCESS", message="제품 규격 후보를 조회했습니다.", data=result)
