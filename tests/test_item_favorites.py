import hashlib
import json
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from alembic import command
from app.api.dependencies import get_current_user_id
from app.db.session import get_db
from app.models.item import SavedItem
from app.models.operation import OperationReceipt
from app.schemas.item import ItemCreate
from app.services.operations import request_fingerprint

pytestmark = pytest.mark.integration
ITEM = {"name": "캠핑 의자", "width_cm": 20, "depth_cm": 30, "height_cm": 40}


@pytest.fixture
def item_client(application, model_session):
    application.dependency_overrides[get_db] = lambda: model_session
    with TestClient(application) as client:
        yield client


def create(client, **overrides):
    response = client.post(
        "/api/v1/items",
        json={**ITEM, **overrides},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def favorite(client, item_id, value, key=None):
    return client.patch(
        f"/api/v1/items/{item_id}/favorite",
        json={"is_favorite": value},
        headers={"Idempotency-Key": str(key or uuid4())},
    )


def test_create_default_and_explicit_favorite(item_client):
    plain = create(item_client)
    marked = create(item_client, name="배낭", is_favorite=True)
    assert plain["is_favorite"] is False
    assert marked["is_favorite"] is True
    response = item_client.get("/api/v1/items").json()["data"]
    assert response["total"] == 2
    assert {item["saved_item_id"] for item in response["items"]} == {
        plain["saved_item_id"],
        marked["saved_item_id"],
    }


def test_filter_counts_pages_and_other_users(item_client, model_session, application):
    first = create(item_client, name="첫 즐겨찾기", is_favorite=True)
    second = create(item_client, name="둘째 즐겨찾기", is_favorite=True)
    plain = create(item_client, name="일반 짐")
    application.dependency_overrides[get_current_user_id] = lambda: "other"
    create(item_client, name="다른 사용자", is_favorite=True)
    application.dependency_overrides.pop(get_current_user_id)
    expected = sorted([first["saved_item_id"], second["saved_item_id"]], reverse=True)
    page = item_client.get("/api/v1/items?favorite=true&limit=1&offset=0").json()["data"]
    assert page["total"] == 2
    assert page["items"][0]["saved_item_id"] == expected[0]
    model_session.rollback()
    page = item_client.get("/api/v1/items?favorite=true&limit=1&offset=1").json()["data"]
    assert page["total"] == 2
    assert page["items"][0]["saved_item_id"] == expected[1]
    model_session.rollback()
    empty_page = item_client.get("/api/v1/items?favorite=true&offset=20").json()["data"]
    assert empty_page["total"] == 2 and empty_page["items"] == []
    model_session.rollback()
    unmarked = item_client.get("/api/v1/items?favorite=false").json()["data"]
    assert unmarked["total"] == 1
    assert unmarked["items"][0]["saved_item_id"] == plain["saved_item_id"]


def test_toggle_only_changes_favorite_and_replay_does_not_undo_later_change(
    item_client, model_session
):
    item = create(item_client)
    key = uuid4()
    first = favorite(item_client, item["saved_item_id"], True, key)
    assert first.status_code == 200, first.text
    assert first.json()["data"] == {**item, "is_favorite": True}
    unmarked = favorite(item_client, item["saved_item_id"], False)
    assert unmarked.status_code == 200
    repeated = favorite(item_client, item["saved_item_id"], True, key)
    assert repeated.json() == first.json()
    stored = model_session.get(SavedItem, UUID(item["saved_item_id"]))
    assert stored.is_favorite is False
    assert stored.name == ITEM["name"]


def test_same_key_cannot_change_body_or_target(item_client):
    first = create(item_client)
    second = create(item_client, name="배낭")
    key = uuid4()
    assert favorite(item_client, first["saved_item_id"], True, key).status_code == 200
    for item_id, value in [(first["saved_item_id"], False), (second["saved_item_id"], True)]:
        response = favorite(item_client, item_id, value, key)
        assert response.status_code == 409
        assert response.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_owner_is_checked_before_replay(item_client, application):
    item = create(item_client)
    key = uuid4()
    assert favorite(item_client, item["saved_item_id"], True, key).status_code == 200
    application.dependency_overrides[get_current_user_id] = lambda: "other"
    response = favorite(item_client, item["saved_item_id"], True, key)
    assert response.status_code == 404
    assert response.json()["code"] == "ITEM_NOT_FOUND"
    missing = favorite(item_client, uuid4(), True)
    assert missing.status_code == 404


@pytest.mark.parametrize("value", [None, "true", 1, 0, [], {}])
def test_favorite_body_requires_boolean(item_client, value):
    response = favorite(item_client, uuid4(), value)
    assert response.status_code == 422


def test_favorite_rejects_missing_extra_fields_and_missing_key(item_client):
    path = f"/api/v1/items/{uuid4()}/favorite"
    for body in [{}, {"is_favorite": True, "user_id": "other"}]:
        response = item_client.patch(path, json=body, headers={"Idempotency-Key": str(uuid4())})
        assert response.status_code == 422
    assert item_client.patch(path, json={"is_favorite": True}).status_code == 422
    assert item_client.get("/api/v1/items?favorite=unknown").status_code == 422


def test_existing_creation_receipt_remains_replayable(item_client, model_session):
    key = uuid4()
    headers = {"Idempotency-Key": str(key)}
    first = item_client.post("/api/v1/items", json=ITEM, headers=headers)
    assert first.status_code == 201
    receipt = model_session.get(OperationReceipt, ("demo-user", "/api/v1/items", key))
    legacy_request = ItemCreate.model_validate(ITEM).model_dump(
        mode="json", exclude={"is_favorite"}
    )
    receipt.request_hash = request_fingerprint(legacy_request)
    receipt.response = {k: v for k, v in receipt.response.items() if k != "is_favorite"}
    model_session.commit()
    repeated = item_client.post("/api/v1/items", json=ITEM, headers=headers)
    assert repeated.status_code == 200
    assert repeated.json() == first.json()


def test_active_duplicate_request_is_rejected(item_client, model_session):
    item = create(item_client)
    key = uuid4()
    digest = hashlib.sha256(
        json.dumps(["demo-user", "/api/v1/items/favorite", str(key)]).encode()
    ).digest()
    lock_id = int.from_bytes(digest[:8], "big", signed=True)
    with model_session.get_bind().engine.begin() as other_connection:
        other_connection.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": lock_id})
        response = favorite(item_client, item["saved_item_id"], True, key)
        assert response.status_code == 409
        assert response.json()["code"] == "REQUEST_IN_PROGRESS"
    assert favorite(item_client, item["saved_item_id"], True, key).status_code == 200


def test_migration_preserves_saved_items_with_false_default(model_session):
    config = model_session.info["migration_config"]
    connection = config.attributes["connection"]
    command.downgrade(config, "0004")
    item_id = connection.scalar(
        text(
            "INSERT INTO saved_item (user_id, name, width_cm, depth_cm, height_cm, "
            "allowed_orientations, transport_state, dimension_source) "
            "VALUES ('legacy', '기존 짐', 20, 30, 40, '[\"WDH\"]'::jsonb, "
            "'UNKNOWN', 'MANUAL') RETURNING saved_item_id"
        )
    )
    command.upgrade(config, "head")
    row = model_session.scalar(select(SavedItem).where(SavedItem.saved_item_id == item_id))
    assert row.name == "기존 짐"
    assert row.is_favorite is False
    command.check(config)
