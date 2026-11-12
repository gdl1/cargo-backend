from uuid import UUID

from sqlalchemy import func, select, true
from sqlalchemy.orm import Session

from app.models.item import SavedItem
from app.schemas.item import ItemCreate, ItemData, ItemListData
from app.services.operations import replay_request, save_response


def _item_data(item: SavedItem) -> ItemData:
    return ItemData.model_validate(item, from_attributes=True)


def create_item(
    session: Session, payload: ItemCreate, user_id: str, key: UUID
) -> tuple[ItemData, bool]:
    with session.begin():
        replay = replay_request(session, user_id, "/api/v1/items", key, payload)
        if replay is not None:
            return ItemData.model_validate(replay), False
        item = SavedItem(user_id=user_id, **payload.model_dump())
        session.add(item)
        session.flush()
        data = _item_data(item)
        save_response(session, user_id, "/api/v1/items", key, payload, data)
    return data, True


def list_items(session: Session, user_id: str, limit: int, offset: int) -> ItemListData:
    page = (
        select(SavedItem)
        .where(SavedItem.user_id == user_id)
        .order_by(SavedItem.created_at.desc(), SavedItem.saved_item_id.desc())
        .limit(limit)
        .offset(offset)
        .cte("item_page")
    )
    total = (
        select(func.count().label("total"))
        .select_from(SavedItem)
        .where(SavedItem.user_id == user_id)
        .subquery()
    )
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
