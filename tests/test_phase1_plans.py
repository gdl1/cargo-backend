from collections.abc import Iterator
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.seed import seed_vehicles
from app.db.session import get_db
from app.models import Calculation, OperationReceipt, Plan, PlanItem, SavedItem
from app.schemas.item import ItemCreate
from app.schemas.plan import PlanCreate, PlanFromResult, PlanMetadataUpdate, PlanUpdate
from app.services.plans import _legacy_request_hash

PLAN = {
    "vehicle_id": "avante_demo",
    "passenger_count": 1,
    "items": [{"name": "의자", "width_cm": 20, "depth_cm": 20, "height_cm": 60, "quantity": 3}],
}


@pytest.mark.parametrize(
    "change",
    [
        {"required_quantity": 4},
        {"required_quantity": True},
        {"required_quantity": -1},
        {"allowed_orientations": []},
        {"allowed_orientations": ["WDH", "WDH"]},
        {"allowed_orientations": ["INVALID"]},
        {"dimension_source": "GUESS"},
        {"transport_state": "CLOSED"},
        {"material": "\x00"},
    ],
)
def test_reject_invalid_item_conditions(change: dict) -> None:
    payload = deepcopy(PLAN)
    payload["items"][0].update(change)
    with pytest.raises(ValidationError):
        PlanCreate.model_validate(payload)


def test_old_plan_inputs_keep_defaults_and_mutable_defaults_are_independent() -> None:
    first, second = [PlanCreate.model_validate(PLAN) for _ in range(2)]
    assert first.passenger_seats is None
    assert first.items[0].required_quantity == 0
    assert first.items[0].dimension_source == "UNSPECIFIED"
    first.items[0].allowed_orientations.clear()
    assert len(second.items[0].allowed_orientations) == 6


@pytest.mark.parametrize(
    "payload",
    [
        {"expected_revision": 1},
        {"expected_revision": True, "plan": PLAN},
        {"expected_revision": 1, "plan": PLAN, "source_preview_id": str(uuid4())},
        {"expected_revision": 1, "source_preview_id": str(uuid4())},
        {
            "expected_revision": 1,
            "source_preview_id": str(uuid4()),
            "source_calculation_id": str(uuid4()),
            "selected_arrangement_id": str(uuid4()),
        },
        {"expected_revision": 1, "plan": PLAN, "selected_arrangement_id": str(uuid4())},
    ],
)
def test_update_requires_exactly_one_input_mode(payload: dict) -> None:
    with pytest.raises(ValidationError):
        PlanUpdate.model_validate(payload)


def test_source_save_and_metadata_empty_validation() -> None:
    with pytest.raises(ValidationError):
        PlanFromResult(selected_arrangement_id=uuid4())
    with pytest.raises(ValidationError):
        PlanMetadataUpdate(expected_revision=1)
    with pytest.raises(ValidationError):
        ItemCreate(name="상자", width_cm=1, depth_cm=1, height_cm=1, dimension_source="SAVED_ITEM")


@pytest.fixture
def client(application: FastAPI, model_session: Session) -> Iterator[TestClient]:
    seed_vehicles(model_session)
    model_session.commit()

    def db():
        yield model_session

    application.dependency_overrides[get_db] = db
    with TestClient(application) as value:
        yield value


def headers(key=None) -> dict:
    return {"Idempotency-Key": str(key or uuid4())}


def create(client: TestClient, session: Session, payload: dict | None = None) -> dict:
    session.commit()
    response = client.post("/api/v1/plans", json=payload or PLAN, headers=headers())
    assert response.status_code == 201, response.text
    return response.json()["data"]


def editable(data: dict) -> dict:
    return {
        key: deepcopy(data[key])
        for key in (
            "vehicle_id",
            "passenger_count",
            "passenger_seats",
            "car_seats",
            "retained_seats",
            "items",
        )
    }


@pytest.mark.integration
def test_edit_reorders_removes_and_keeps_history_with_exact_retry(
    client: TestClient, model_session: Session
) -> None:
    payload = deepcopy(PLAN)
    payload["items"] += [
        {"name": "배낭", "width_cm": 10, "depth_cm": 10, "height_cm": 10, "quantity": 1}
    ]
    original = create(client, model_session, payload)
    plan_id = original["plan_id"]
    calculation = Calculation(
        plan_id=UUID(plan_id), is_success=False, current_folded_segments=None, plan_revision=1
    )
    model_session.add(calculation)
    model_session.commit()
    inputs = editable(original)
    removed = inputs["items"].pop(0)
    inputs["items"][0]["quantity"] = 2
    inputs["items"].append(
        {"name": "새 짐", "width_cm": 12, "depth_cm": 13, "height_cm": 14, "quantity": 1}
    )
    request = {"expected_revision": 1, "plan": inputs}
    key = headers()
    changed = client.put(f"/api/v1/plans/{plan_id}", json=request, headers=key)
    assert changed.status_code == 200, changed.text
    data = changed.json()["data"]
    assert data["revision"] == 2 and data["created_at"] == original["created_at"]
    assert data["items"][0]["item_id"] == original["items"][1]["item_id"]
    assert data["latest_calculation_id"] is None
    replay = client.put(f"/api/v1/plans/{plan_id}", json=request, headers=key)
    assert replay.json() == changed.json()
    assert (
        client.put(f"/api/v1/plans/{plan_id}", json=request, headers=headers()).status_code == 409
    )
    old_item = model_session.get(PlanItem, UUID(removed["item_id"]))
    assert old_item.is_active is False
    model_session.refresh(calculation)
    assert calculation.result_payload["input_snapshot"]["items"][0]["name"] == "의자"
    assert calculation.result_payload["input_snapshot"]["items"][0]["quantity"] == 3
    model_session.commit()
    summary = client.get("/api/v1/plans").json()["data"]["plans"][0]
    assert summary["item_type_count"] == 2 and summary["item_total_quantity"] == 3
    assert [item["name"] for item in summary["item_summaries"]] == ["배낭", "새 짐"]


@pytest.mark.integration
def test_create_retry_after_edit_returns_creation_snapshot(
    client: TestClient, model_session: Session
) -> None:
    key = headers()
    first = client.post("/api/v1/plans", json=PLAN, headers=key)
    original = first.json()["data"]
    inputs = editable(original)
    inputs["items"][0]["name"] = "변경된 이름"
    changed = client.put(
        f"/api/v1/plans/{original['plan_id']}",
        json={"expected_revision": 1, "plan": inputs},
        headers=headers(),
    )
    assert changed.status_code == 200, changed.text
    retry = client.post("/api/v1/plans", json=PLAN, headers=key)
    assert retry.status_code == 200 and retry.json() == first.json()


@pytest.mark.integration
def test_legacy_creation_key_rejects_new_constraints_and_replays_after_edit(
    client: TestClient, model_session: Session
) -> None:
    key = uuid4()
    first = client.post("/api/v1/plans", json=PLAN, headers=headers(key)).json()["data"]
    plan = model_session.get(Plan, UUID(first["plan_id"]))
    plan.request_hash = _legacy_request_hash(PlanCreate.model_validate(PLAN))
    receipt = model_session.get(OperationReceipt, (plan.user_id, "/api/v1/plans", key))
    model_session.delete(receipt)
    model_session.commit()
    altered = deepcopy(PLAN)
    altered["items"][0]["required_quantity"] = 1
    bad = client.post("/api/v1/plans", json=altered, headers=headers(key))
    assert bad.status_code == 409
    inputs = editable(first)
    inputs["items"][0]["quantity"] = 2
    changed = client.put(
        f"/api/v1/plans/{first['plan_id']}",
        json={"expected_revision": 1, "plan": inputs},
        headers=headers(),
    )
    assert changed.status_code == 200, changed.text
    retry = client.post("/api/v1/plans", json=PLAN, headers=headers(key))
    assert retry.status_code == 200 and retry.json()["data"] == first


@pytest.mark.integration
def test_foreign_item_and_removed_item_ids_cannot_be_reused(
    client: TestClient, model_session: Session
) -> None:
    first, other = [create(client, model_session) for _ in range(2)]
    inputs = editable(first)
    inputs["items"][0]["item_id"] = other["items"][0]["item_id"]
    response = client.put(
        f"/api/v1/plans/{first['plan_id']}",
        json={"expected_revision": 1, "plan": inputs},
        headers=headers(),
    )
    assert response.status_code == 422
    inputs["items"].append(deepcopy(inputs["items"][0]))
    assert (
        client.put(
            f"/api/v1/plans/{first['plan_id']}",
            json={"expected_revision": 1, "plan": inputs},
            headers=headers(),
        ).status_code
        == 422
    )


@pytest.mark.integration
def test_metadata_and_favorite_filters_preserve_input_revision(
    client: TestClient, model_session: Session
) -> None:
    first = create(client, model_session, {**PLAN, "name": "첫 여행", "category": "TRAVEL"})
    second = create(client, model_session, {**PLAN, "name": "다음 이사", "category": "MOVING"})
    for original in (second, first):
        model_session.commit()
        result = client.patch(
            f"/api/v1/plans/{original['plan_id']}/metadata",
            json={"expected_revision": 1, "is_favorite": True},
            headers=headers(),
        )
        assert result.status_code == 200, result.text
        assert result.json()["data"]["revision"] == 1
    response = client.get(
        "/api/v1/plans", params={"favorite": "true", "sort": "favorite", "limit": 1}
    )
    data = response.json()["data"]
    assert data["total"] == 2 and data["plans"][0]["plan_id"] == first["plan_id"]
    filtered = client.get("/api/v1/plans", params={"category": "MOVING", "q": "이사"}).json()[
        "data"
    ]
    assert filtered["total"] == 1 and filtered["plans"][0]["plan_id"] == second["plan_id"]
    assert client.get("/api/v1/plans", params={"q": "%"}).json()["data"]["total"] == 0


@pytest.mark.integration
def test_saved_item_owner_snapshot_and_default_quantity(
    client: TestClient, model_session: Session, settings: Settings
) -> None:
    payload = {
        "name": "의자",
        "width_cm": 20,
        "depth_cm": 20,
        "height_cm": 60,
        "default_quantity": 3,
        "dimension_source": "AR",
    }
    key = headers()
    first = client.post("/api/v1/items", json=payload, headers=key)
    assert first.status_code == 201, first.text
    assert client.post("/api/v1/items", json=payload, headers=key).json() == first.json()
    saved_id = first.json()["data"]["saved_item_id"]
    plan_payload = deepcopy(PLAN)
    plan_payload["items"][0].update(
        saved_item_id=saved_id, dimension_source="SAVED_ITEM", quantity=1
    )
    plan = create(client, model_session, plan_payload)
    assert plan["items"][0]["quantity"] == 1
    assert client.get("/api/v1/items").json()["data"]["items"][0]["default_quantity"] == 3
    saved = model_session.get(SavedItem, UUID(saved_id))
    saved.user_id = "other-user"
    model_session.commit()
    assert client.get("/api/v1/items").json()["data"]["total"] == 0
    model_session.commit()
    assert client.post("/api/v1/plans", json=plan_payload, headers=headers()).status_code == 422
    assert model_session.scalar(select(func.count()).select_from(SavedItem)) == 1


@pytest.mark.integration
@pytest.mark.parametrize(
    "seats", [["ROW_1_RIGHT"], ["ROW_1_LEFT", "ROW_1_RIGHT"], ["ROW_1_LEFT", "ROW_1_LEFT"]]
)
def test_invalid_passenger_selection_rejected(client: TestClient, seats: list[str]) -> None:
    response = client.post(
        "/api/v1/plans", json={**PLAN, "passenger_seats": seats}, headers=headers()
    )
    assert response.status_code == 422


@pytest.mark.integration
def test_preview_apply_updates_revision_and_keeps_old_results(
    client: TestClient, model_session: Session
) -> None:
    original = create(
        client,
        model_session,
        {
            **PLAN,
            "items": [
                {"name": "상자", "width_cm": 10, "depth_cm": 10, "height_cm": 10, "quantity": 1}
            ],
        },
    )
    path = f"/api/v1/plans/{original['plan_id']}"
    old = client.post(f"{path}/calculations", json={}, headers=headers()).json()["data"]
    assert old["result_code"] == "LAYOUT_FOUND"
    inputs = editable(original)
    inputs["items"][0]["quantity"] = 2
    preview_response = client.post(
        "/api/v1/calculations/preview",
        json={"source_plan_id": original["plan_id"], "expected_revision": 1, "plan": inputs},
        headers=headers(),
    )
    assert preview_response.status_code == 201, preview_response.text
    preview = preview_response.json()["data"]
    candidate = preview["calculation"]["arrangements"][0]
    request = {
        "expected_revision": 1,
        "source_preview_id": preview["preview_id"],
        "selected_arrangement_id": candidate["arrangement_id"],
    }
    key = headers()
    changed = client.put(path, json=request, headers=key)
    assert changed.status_code == 200, changed.text
    data = changed.json()["data"]
    assert data["revision"] == 2 and data["items"][0]["quantity"] == 2
    assert data["selected_calculation_id"] != old["calculation_id"]
    assert data["selected_calculation_id"] == data["latest_calculation_id"]
    assert client.put(path, json=request, headers=key).json() == changed.json()
    copied = client.get(f"{path}/calculations/{data['selected_calculation_id']}").json()["data"]
    assert len(copied["arrangements"][0]["placements"]) == 2
    assert copied["input_snapshot"]["items"][0]["quantity"] == 2
    read_old = client.get(f"{path}/calculations/{old['calculation_id']}").json()["data"]
    assert read_old == old and read_old["input_snapshot"]["items"][0]["quantity"] == 1
    model_session.rollback()
    invalid = client.put(
        f"{path}/selection",
        json={
            "expected_revision": 2,
            "calculation_id": old["calculation_id"],
            "arrangement_id": old["arrangements"][0]["arrangement_id"],
        },
        headers=headers(),
    )
    assert invalid.status_code == 409


@pytest.mark.integration
def test_result_apply_failure_rolls_back_plan_and_receipt(
    client: TestClient, model_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = create(
        client,
        model_session,
        {
            **PLAN,
            "items": [
                {"name": "상자", "width_cm": 10, "depth_cm": 10, "height_cm": 10, "quantity": 1}
            ],
        },
    )
    path = f"/api/v1/plans/{original['plan_id']}"
    inputs = editable(original)
    inputs["items"][0]["quantity"] = 2
    preview = client.post(
        "/api/v1/calculations/preview",
        json={"source_plan_id": original["plan_id"], "expected_revision": 1, "plan": inputs},
        headers=headers(),
    ).json()["data"]
    request = {
        "expected_revision": 1,
        "source_preview_id": preview["preview_id"],
        "selected_arrangement_id": preview["calculation"]["arrangements"][0]["arrangement_id"],
    }

    def fail(*args, **kwargs):
        raise RuntimeError("after-plan-flush")

    key = headers()
    with monkeypatch.context() as patch:
        patch.setattr("app.services.calculations.persist_applied_result", fail)
        with pytest.raises(RuntimeError, match="after-plan-flush"):
            client.put(path, json=request, headers=key)
    read = client.get(path).json()["data"]
    assert read == original
    model_session.rollback()
    retried = client.put(path, json=request, headers=key)
    assert retried.status_code == 200 and retried.json()["data"]["revision"] == 2


@pytest.mark.integration
def test_selected_arrangement_does_not_modify_input_and_replay_is_stable(
    client: TestClient, model_session: Session
) -> None:
    original = create(
        client,
        model_session,
        {
            **PLAN,
            "items": [
                {"name": "상자", "width_cm": 10, "depth_cm": 10, "height_cm": 10, "quantity": 1}
            ],
        },
    )
    path = f"/api/v1/plans/{original['plan_id']}"
    result = client.post(f"{path}/calculations", json={}, headers=headers()).json()["data"]
    request = {
        "expected_revision": 1,
        "calculation_id": result["calculation_id"],
        "arrangement_id": result["arrangements"][0]["arrangement_id"],
    }
    key = headers()
    selected = client.put(f"{path}/selection", json=request, headers=key)
    assert selected.status_code == 200, selected.text
    assert selected.json()["data"]["revision"] == 1
    read = client.get(path).json()["data"]
    assert (
        read["selected_arrangement_id"] == request["arrangement_id"]
        and read["items"] == original["items"]
    )
    model_session.rollback()
    changed = client.put(
        path, json={"expected_revision": 1, "plan": editable(original)}, headers=headers()
    )
    assert changed.status_code == 200
    assert changed.json()["data"]["selected_arrangement_id"] is None
    retry = client.put(f"{path}/selection", json=request, headers=key)
    assert retry.json() == selected.json()
    current = client.get(path).json()["data"]
    assert current["revision"] == 2 and current["selected_arrangement_id"] is None


@pytest.mark.integration
def test_selection_rejects_old_vehicle_model_and_preserves_current_layout(
    client: TestClient, model_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.vehicles import get_vehicle_catalog

    original = create(
        client,
        model_session,
        {
            **PLAN,
            "items": [
                {"name": "상자", "width_cm": 10, "depth_cm": 10, "height_cm": 10, "quantity": 1}
            ],
        },
    )
    path = f"/api/v1/plans/{original['plan_id']}"
    old_response = client.post(f"{path}/calculations", json={}, headers=headers())
    assert old_response.status_code == 201, old_response.text
    old = old_response.json()["data"]
    vehicle = get_vehicle_catalog()[original["vehicle_id"]].model_copy(
        update={"data_version": "avante-demo-updated"}
    )
    monkeypatch.setattr("app.services.vehicles._load_vehicle_data", lambda: (vehicle,))
    current_response = client.post(f"{path}/calculations", json={}, headers=headers())
    assert current_response.status_code == 201, current_response.text
    current = current_response.json()["data"]
    assert current["vehicle_data_version"] != old["vehicle_data_version"]
    current_selection = {
        "expected_revision": 1,
        "calculation_id": current["calculation_id"],
        "arrangement_id": current["arrangements"][0]["arrangement_id"],
    }
    selected = client.put(f"{path}/selection", json=current_selection, headers=headers())
    assert selected.status_code == 200, selected.text
    failed_key = uuid4()
    stale = client.put(
        f"{path}/selection",
        json={
            "expected_revision": 1,
            "calculation_id": old["calculation_id"],
            "arrangement_id": old["arrangements"][0]["arrangement_id"],
        },
        headers=headers(failed_key),
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "BASE_LAYOUT_STALE"
    read = client.get(path).json()["data"]
    assert read["selected_calculation_id"] == current["calculation_id"]
    assert read["selected_arrangement_id"] == current_selection["arrangement_id"]
    assert read["revision"] == 1 and read["items"] == original["items"]
    assert (
        model_session.scalar(select(OperationReceipt).where(OperationReceipt.key == failed_key))
        is None
    )
    model_session.rollback()
    retry = client.put(f"{path}/selection", json=current_selection, headers=headers(failed_key))
    assert retry.status_code == 200, retry.text
    assert retry.json() == selected.json()
    kept = client.post(
        f"{path}/calculations",
        json={
            "packing_mode": "KEEP_EXISTING",
            "cargo_state": "SELECTED_ARRANGEMENT",
            "base_calculation_id": current["calculation_id"],
            "base_arrangement_id": current_selection["arrangement_id"],
        },
        headers=headers(),
    )
    assert kept.status_code == 201, kept.text
    assert kept.json()["data"]["result_code"] == "LAYOUT_FOUND"
