from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.exceptions import AppError
from app.db.seed import seed_vehicles
from app.db.session import get_db
from app.models import Calculation, Plan
from app.models.preview import CalculationPreview
from app.schemas.calculation import CalculationCreate, PreviewCreate
from app.schemas.plan import PlanCreate
from app.services.calculations import create_calculation, load_candidate
from app.services.plans import create_plan
from app.services.previews import create_preview

pytestmark = pytest.mark.integration
PLAN = {
    "vehicle_id": "avante_demo",
    "passenger_count": 1,
    "items": [{"name": "첫 상자", "width_cm": 10, "depth_cm": 10, "height_cm": 10, "quantity": 1}],
}


@pytest.fixture
def calculation_client(application, model_session) -> Iterator[TestClient]:
    seed_vehicles(model_session)
    model_session.commit()
    application.dependency_overrides[get_db] = lambda: model_session
    with TestClient(application) as client:
        yield client


def post(client, url, payload, key=None):
    return client.post(url, json=payload, headers={"Idempotency-Key": str(key or uuid4())})


def test_calculation_is_immutable_and_replay_precedes_revision(calculation_client, model_session):
    client = calculation_client
    plan = post(client, "/api/v1/plans", PLAN).json()["data"]
    key = uuid4()
    path = f"/api/v1/plans/{plan['plan_id']}/calculations"
    result = post(client, path, {"expected_revision": 1}, key)
    assert result.status_code == 201, result.text
    data = result.json()["data"]
    assert data["result_code"] == "LAYOUT_FOUND"
    assert data["input_snapshot"]["items"][0]["name"] == "첫 상자"
    row = model_session.get(Plan, UUID(plan["plan_id"]))
    row.revision += 1
    row.items[0].name = "변경한 상자"
    model_session.commit()
    repeated = post(client, path, {"expected_revision": 1}, key)
    assert repeated.status_code == 200
    assert repeated.json() == result.json()
    read = client.get(f"{path}/{data['calculation_id']}")
    assert read.status_code == 200
    assert read.json()["data"] == data
    model_session.rollback()
    conflict = post(client, path, {"expected_revision": 1})
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "PLAN_REVISION_CONFLICT"


def test_preview_does_not_create_plan_and_retries_same_response(calculation_client, model_session):
    client = calculation_client
    key = uuid4()
    payload = {"plan": PLAN, "calculation_options": {"cargo_state": "UNKNOWN"}}
    first = post(client, "/api/v1/calculations/preview", payload, key)
    assert first.status_code == 201, first.text
    data = first.json()["data"]
    assert data["calculation"]["result_code"] == "STATE_REQUIRED"
    assert data["calculation"]["plan_id"] is None
    assert data["calculation"]["calculation_id"] is None
    assert data["calculation"]["input_snapshot"]["items"][0]["item_id"]
    second = post(client, "/api/v1/calculations/preview", payload, key)
    assert second.status_code == 200
    assert second.json() == first.json()
    assert model_session.scalar(select(func.count()).select_from(Plan)) == 0
    assert model_session.scalar(select(func.count()).select_from(Calculation)) == 0
    assert model_session.scalar(select(func.count()).select_from(CalculationPreview)) == 1


def test_preview_expiry_and_owner_are_checked(model_session, settings):
    seed_vehicles(model_session)
    model_session.commit()
    data, _ = create_preview(
        model_session,
        PreviewCreate.model_validate({"plan": PLAN}),
        "owner",
        uuid4(),
        settings,
        stacking_assessments=[],
    )
    candidate = data.calculation.arrangements[0]
    with pytest.raises(AppError) as inaccessible:
        load_candidate(
            model_session,
            "other",
            source_preview_id=data.preview_id,
            arrangement_id=UUID(candidate["arrangement_id"]),
        )
    assert inaccessible.value.code == "PREVIEW_NOT_FOUND"
    model_session.rollback()
    preview = model_session.get(CalculationPreview, data.preview_id)
    preview.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    model_session.commit()
    with pytest.raises(AppError) as expired:
        load_candidate(
            model_session,
            "owner",
            source_preview_id=data.preview_id,
            arrangement_id=UUID(candidate["arrangement_id"]),
        )
    assert (expired.value.status_code, expired.value.code) == (410, "PREVIEW_EXPIRED")


def test_plan_change_during_calculation_prevents_save(model_session, settings, monkeypatch):
    seed_vehicles(model_session)
    model_session.commit()
    plan, _ = create_plan(model_session, PlanCreate.model_validate(PLAN), "owner", uuid4())

    def mutate(*args, **kwargs):
        row = model_session.get(Plan, plan.plan_id)
        row.revision += 1
        model_session.commit()
        return {
            "result_code": "TIME_LIMIT",
            "is_success": False,
            "search_complete": False,
            "arrangements": [],
            "alternatives": [],
        }

    monkeypatch.setattr("app.services.calculations.run_calculation", mutate)
    with pytest.raises(AppError) as changed:
        create_calculation(
            model_session, CalculationCreate(), "owner", plan.plan_id, uuid4(), settings
        )
    assert changed.value.code == "PLAN_REVISION_CONFLICT"
    assert model_session.scalar(select(func.count()).select_from(Calculation)) == 0


def test_independent_preview_cannot_overwrite_existing_plan(model_session, settings):
    seed_vehicles(model_session)
    model_session.commit()
    plan, _ = create_plan(model_session, PlanCreate.model_validate(PLAN), "owner", uuid4())
    preview, _ = create_preview(
        model_session,
        PreviewCreate.model_validate({"plan": PLAN}),
        "owner",
        uuid4(),
        settings,
        stacking_assessments=[],
    )
    target = model_session.get(Plan, plan.plan_id)
    with pytest.raises(AppError) as invalid:
        load_candidate(
            model_session,
            "owner",
            source_preview_id=preview.preview_id,
            arrangement_id=UUID(preview.calculation.arrangements[0]["arrangement_id"]),
            target_plan=target,
            expected_revision=1,
        )
    assert invalid.value.code == "BASE_LAYOUT_STALE"


def test_preview_candidate_copy_remaps_ids_and_preserves_history(calculation_client, model_session):
    client = calculation_client
    response = post(client, "/api/v1/calculations/preview", {"plan": PLAN})
    assert response.status_code == 201, response.text
    preview = response.json()["data"]
    candidate = preview["calculation"]["arrangements"][0]
    response = post(
        client,
        "/api/v1/plans",
        {
            "source_preview_id": preview["preview_id"],
            "selected_arrangement_id": candidate["arrangement_id"],
        },
    )
    assert response.status_code == 201, response.text
    plan = response.json()["data"]
    path = f"/api/v1/plans/{plan['plan_id']}/calculations/{plan['selected_calculation_id']}"
    copied = client.get(path).json()["data"]
    placement = copied["arrangements"][0]["placements"][0]
    original = candidate["placements"][0]
    assert placement["placement_id"] != original["placement_id"]
    assert placement["item_id"] != original["item_id"]
    assert placement["source_item_id"] == original["item_id"]
    assert placement["source_unit_index"] == original["unit_index"]
    assert placement["item_id"] == plan["items"][0]["item_id"]
    assert copied["provenance"]["source_preview_id"] == preview["preview_id"]
    model_session.rollback()
    again = post(
        client,
        "/api/v1/plans",
        {
            "source_calculation_id": copied["calculation_id"],
            "selected_arrangement_id": copied["arrangements"][0]["arrangement_id"],
        },
    )
    assert again.status_code == 201, again.text
    original_preview = model_session.get(CalculationPreview, UUID(preview["preview_id"]))
    assert original_preview.result_payload == preview["calculation"]


def test_exclusion_copy_remaps_stack_support_and_keeps_selected_layout(
    calculation_client, model_session, monkeypatch
):
    def allow_stacking(items, settings):
        item_id = items[0]["item_id"]
        return [
            {
                "lower_item_id": item_id,
                "upper_item_id": item_id,
                "allowed": True,
                "basis": "LLM_ESTIMATE",
                "reason": "테스트 조합",
            }
        ]

    monkeypatch.setattr("app.services.llm.estimate_stacking", allow_stacking)
    client = calculation_client
    request = {
        "plan": {
            "vehicle_id": "avante_demo",
            "passenger_count": 5,
            "items": [
                {
                    "name": "큰 상자",
                    "width_cm": 60,
                    "depth_cm": 70,
                    "height_cm": 20,
                    "quantity": 3,
                    "required_quantity": 1,
                    "allowed_orientations": ["WDH"],
                }
            ],
        },
        "calculation_options": {"allow_exclusions": True},
    }
    response = post(client, "/api/v1/calculations/preview", request)
    assert response.status_code == 201, response.text
    preview = response.json()["data"]
    result = preview["calculation"]
    assert result["result_code"] == "EXCLUSION_OPTIONS_FOUND"
    alternative = result["alternatives"][0]
    assert alternative["excluded_total_quantity"] == 1
    assert any(p["supporting_placement_ids"] for p in alternative["placements"])
    saved = post(
        client,
        "/api/v1/plans",
        {
            "source_preview_id": preview["preview_id"],
            "selected_arrangement_id": alternative["arrangement_id"],
        },
    )
    assert saved.status_code == 201, saved.text
    plan = saved.json()["data"]
    assert plan["items"][0]["quantity"] == 2
    path = f"/api/v1/plans/{plan['plan_id']}/calculations"
    result = client.get(f"{path}/{plan['selected_calculation_id']}").json()["data"]
    candidate = result["arrangements"][0]
    placements = candidate["placements"]
    assert sorted(p["unit_index"] for p in placements) == [1, 2]
    ids = {p["placement_id"] for p in placements}
    assert all(set(p["supporting_placement_ids"]) <= ids for p in placements)
    assert candidate["excluded_total_quantity"] == 0
    assert not candidate.get("exclusions")
    model_session.rollback()
    keep = post(
        client,
        path,
        {
            "packing_mode": "KEEP_EXISTING",
            "cargo_state": "SELECTED_ARRANGEMENT",
            "base_calculation_id": plan["selected_calculation_id"],
            "base_arrangement_id": plan["selected_arrangement_id"],
            "expected_revision": plan["revision"],
        },
    )
    assert keep.status_code == 201, keep.text
    kept = keep.json()["data"]
    assert kept["result_code"] == "LAYOUT_FOUND"
    actual = kept["arrangements"][0]["placements"]

    def geometry(rows):
        return sorted(
            (
                p["item_id"],
                p["unit_index"],
                p["space_id"],
                p["x_cm"],
                p["y_cm"],
                p["z_cm"],
                p["orientation"],
            )
            for p in rows
        )

    assert geometry(actual) == geometry(placements)
    copied_again = post(
        client,
        "/api/v1/plans",
        {
            "source_calculation_id": kept["calculation_id"],
            "selected_arrangement_id": kept["arrangements"][0]["arrangement_id"],
        },
    )
    assert copied_again.status_code == 201, copied_again.text
    new_plan = copied_again.json()["data"]
    again = post(
        client,
        "/api/v1/plans",
        {
            "source_calculation_id": new_plan["selected_calculation_id"],
            "selected_arrangement_id": new_plan["selected_arrangement_id"],
        },
    )
    assert again.status_code == 201, again.text
