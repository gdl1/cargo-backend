from uuid import UUID

from sqlalchemy import func, select, true
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.models.item import SavedItem
from app.schemas.item import ItemCreate, ItemData, ItemFavoriteUpdate, ItemListData
from app.services.operations import replay_request, save_response


def _item_data(item: SavedItem) -> ItemData:
    return ItemData.model_validate(item, from_attributes=True)


def create_item(
    session: Session, payload: ItemCreate, user_id: str, key: UUID
) -> tuple[ItemData, bool]:
    # 기존 등록 요청의 재시도 키는 기본값 필드 추가 후에도 유지합니다.
    request = payload.model_dump(
        mode="json", exclude={"is_favorite"} if not payload.is_favorite else set()
    )
    with session.begin():
        replay = replay_request(session, user_id, "/api/v1/items", key, request)
        if replay is not None:
            return ItemData.model_validate(replay), False
        item = SavedItem(user_id=user_id, **payload.model_dump())
        session.add(item)
        session.flush()
        data = _item_data(item)
        save_response(session, user_id, "/api/v1/items", key, request, data)
    return data, True


def list_items(
    session: Session, user_id: str, limit: int, offset: int, favorite: bool | None = None
) -> ItemListData:
    filters = [SavedItem.user_id == user_id]
    if favorite is not None:
        filters.append(SavedItem.is_favorite == favorite)
    page = (
        select(SavedItem)
        .where(*filters)
        .order_by(SavedItem.created_at.desc(), SavedItem.saved_item_id.desc())
        .limit(limit)
        .offset(offset)
        .cte("item_page")
    )
    total = select(func.count().label("total")).select_from(SavedItem).where(*filters).subquery()
    rows = (
        session.execute(
            select(total.c.total, page)
            .select_from(total.outerjoin(page, true()))
            .order_by(page.c.created_at.desc(), page.c.saved_item_id.desc())
        )
        .mappings()
        .all()
    )
    return ItemListData(
        items=[
            ItemData.model_validate(
                {key: value for key, value in row.items() if key not in {"total", "user_id"}}
            )
            for row in rows
            if row["saved_item_id"] is not None
        ],
        limit=limit,
        offset=offset,
        total=rows[0]["total"],
    )


def update_item_favorite(
    session: Session,
    user_id: str,
    saved_item_id: UUID,
    payload: ItemFavoriteUpdate,
    key: UUID,
) -> ItemData:
    scope = "/api/v1/items/favorite"
    request = {"saved_item_id": str(saved_item_id), **payload.model_dump()}
    with session.begin():
        item = session.scalar(
            select(SavedItem)
            .where(SavedItem.saved_item_id == saved_item_id, SavedItem.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if item is None:
            raise AppError(404, "ITEM_NOT_FOUND", "저장한 짐을 찾을 수 없습니다.")
        replay = replay_request(session, user_id, scope, key, request)
        if replay is not None:
            return ItemData.model_validate(replay)
        item.is_favorite = payload.is_favorite
        session.flush()
        data = _item_data(item)
        save_response(session, user_id, scope, key, request, data)
    return data
