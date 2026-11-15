from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import func, select, true
from sqlalchemy.orm import Session, aliased

from app.core.exceptions import AppError
from app.models.item import SavedItem
from app.schemas.item import ItemCreate, ItemData, ItemFavoriteUpdate, ItemListData, ItemPhotoData
from app.services.item_photos import ItemPhotoStorage, validate_item_photo
from app.services.operations import replay_request, save_response


def _item_photo_path(saved_item_id: UUID) -> str:
    return f"/api/v1/items/{saved_item_id}/photo"


def _item_data(item: SavedItem) -> ItemData:
    data = ItemData.model_validate(item, from_attributes=True)
    if item.photo_object_key is not None:
        data.photo_url = _item_photo_path(item.saved_item_id)
    return data


def _lock_owned_item(
    session: Session, user_id: str, saved_item_id: UUID, *, read_lock: bool = False
) -> SavedItem:
    item = session.scalar(
        select(SavedItem)
        .where(SavedItem.saved_item_id == saved_item_id, SavedItem.user_id == user_id)
        .with_for_update(read=read_lock)
        .execution_options(populate_existing=True)
    )
    if item is None:
        raise AppError(404, "ITEM_NOT_FOUND", "저장한 짐을 찾을 수 없습니다.")
    return item


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
    paged_item = aliased(SavedItem, page)
    rows = session.execute(
        select(total.c.total, paged_item)
        .select_from(total.outerjoin(page, true()))
        .order_by(page.c.created_at.desc(), page.c.saved_item_id.desc())
    ).all()
    return ItemListData(
        items=[_item_data(item) for _, item in rows if item is not None],
        limit=limit,
        offset=offset,
        total=rows[0].total,
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
        item = _lock_owned_item(session, user_id, saved_item_id)
        replay = replay_request(session, user_id, scope, key, request)
        if replay is not None:
            return ItemData.model_validate(replay)
        item.is_favorite = payload.is_favorite
        session.flush()
        data = _item_data(item)
        save_response(session, user_id, scope, key, request, data)
    return data


def save_item_photo(
    session: Session,
    user_id: str,
    saved_item_id: UUID,
    key: UUID,
    *,
    storage: ItemPhotoStorage,
    content: bytes,
    content_type: str,
) -> ItemPhotoData:
    validate_item_photo(content, content_type)
    scope = "/api/v1/items/photo"
    request = {
        "saved_item_id": str(saved_item_id),
        "content_type": content_type,
        "sha256": sha256(content).hexdigest(),
    }
    with session.begin():
        item = _lock_owned_item(session, user_id, saved_item_id)
        replay = replay_request(session, user_id, scope, key, request)
        if replay is not None:
            return ItemPhotoData.model_validate(replay)
        previous_key = item.photo_object_key
        object_key = f"item-photos/{saved_item_id}/{uuid4().hex}"
        data = ItemPhotoData(saved_item_id=saved_item_id, photo_url=_item_photo_path(saved_item_id))
        storage.put(object_key, content, content_type)
        try:
            item.photo_object_key = object_key
            save_response(session, user_id, scope, key, request, data)
        except Exception:
            storage.discard(object_key)
            raise
    # 새 연결을 커밋한 뒤 이전 객체를 지웁니다.
    if previous_key is not None:
        storage.discard(previous_key)
    return data


def read_item_photo(
    session: Session, user_id: str, saved_item_id: UUID, storage: ItemPhotoStorage
) -> tuple[bytes, str]:
    with session.begin():
        # 읽는 동안 사진 교체가 기존 객체를 지우지 못하게 합니다.
        item = _lock_owned_item(session, user_id, saved_item_id, read_lock=True)
        if item.photo_object_key is None:
            raise AppError(404, "ITEM_PHOTO_NOT_FOUND", "저장한 짐에 연결된 사진이 없습니다.")
        return storage.get(item.photo_object_key)
