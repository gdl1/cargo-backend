from copy import deepcopy
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.seed import seed_vehicles
from app.db.session import get_db
from app.models import Calculation, Plan
from app.models.operation import OperationReceipt
from app.models.preview import CalculationPreview
from app.schemas.vehicle import VehicleDetail
from app.services.llm import LlmUnavailable, ResponsesClient
from app.services.operations import request_guard
from app.services.vehicles import get_vehicle_catalog

pytestmark = pytest.mark.integration
PATH = "/api/v1/ai/loading-guides"
FALLBACK = "위 그림을 보고 배치해 주세요."
PLAN = {
    "vehicle_id": "avante_demo",
    "passenger_count": 1,
    "items": [
        {"name": "운반 상자", "width_cm": 10, "depth_cm": 10, "height_cm": 10, "quantity": 2}
    ],
}


@pytest.fixture
def guide_app(application, model_session, monkeypatch):
    seed_vehicles(model_session)
    model_session.commit()
    application.dependency_overrides[get_db] = lambda: model_session
    monkeypatch.setattr("app.services.llm.estimate_stacking", lambda *args, **kwargs: [])
    with TestClient(application) as client:
        yield SimpleNamespace(client=client, session=model_session, monkeypatch=monkeypatch)


def post(client, path, payload, key=None):
    return client.post(path, json=payload, headers={"Idempotency-Key": str(key or uuid4())})


def source(app, kind="saved", plan_input=PLAN):
    if kind == "saved":
        response = post(app.client, "/api/v1/plans", plan_input)
        assert response.status_code == 201, response.text
        plan = response.json()["data"]
        response = post(app.client, f"/api/v1/plans/{plan['plan_id']}/calculations", {})
        assert response.status_code == 201, response.text
        calculation = response.json()["data"]
        reference = {"plan_id": plan["plan_id"], "calculation_id": calculation["calculation_id"]}
    else:
        response = post(app.client, "/api/v1/calculations/preview", {"plan": plan_input})
        assert response.status_code == 201, response.text
        preview = response.json()["data"]
        calculation = preview["calculation"]
        reference = {"preview_id": preview["preview_id"]}
    assert calculation["result_code"] == "LAYOUT_FOUND"
    candidate = calculation["arrangements"][0]
    reference["arrangement_id"] = candidate["arrangement_id"]
    return reference, calculation, candidate


def install_model(app, reply="선택한 배치의 순서대로 짐을 놓아 주세요.", side_effect=None):
    calls = []

    def generate(client, name, model, instructions, data):
        calls.append({"name": name, "instructions": instructions, "data": deepcopy(data)})
        if side_effect:
            side_effect()
        return model.model_validate({"reply": reply})

    app.monkeypatch.setattr(ResponsesClient, "call_function", generate)
    return calls


@pytest.mark.parametrize("kind", ["saved", "preview"])
def test_guide_explains_exact_selected_layout_without_changing_plan(guide_app, kind):
    app = guide_app
    reference, calculation, candidate = source(app, kind)
    calls = install_model(app)
    response = post(app.client, PATH, reference)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["source"] == "AI"
    assert data["arrangement_id"] == candidate["arrangement_id"]
    assert data["preview_id"] == reference.get("preview_id")
    assert data["plan_id"] == reference.get("plan_id")
    assert data["calculation_id"] == reference.get("calculation_id")
    assert [(s["order"], s["placement_id"]) for s in data["steps"]] == [
        (p["load_order"], p["placement_id"])
        for p in sorted(candidate["placements"], key=lambda p: p["load_order"])
    ]
    assert len(calls) == 1
    with app.session.begin():
        if kind == "saved":
            row = app.session.get(Calculation, UUID(reference["calculation_id"]))
            assert row.result_payload == calculation
            plan = app.session.get(Plan, UUID(reference["plan_id"]))
            assert plan.revision == 1
            assert plan.selected_calculation_id is None
            assert app.session.scalar(select(func.count()).select_from(Calculation)) == 1
        else:
            row = app.session.get(CalculationPreview, UUID(reference["preview_id"]))
            assert row.result_payload == calculation
            assert app.session.scalar(select(func.count()).select_from(Plan)) == 0


def test_guide_uses_historical_item_snapshot_after_plan_edit(guide_app):
    app = guide_app
    reference, _, _ = source(app)
    with app.session.begin():
        plan = app.session.get(Plan, UUID(reference["plan_id"]))
        plan.items[0].name = "현재 바뀐 짐"
        plan.items[0].width_cm = 99
        plan.revision += 1
    calls = install_model(app)
    response = post(app.client, PATH, reference)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["source"] == "AI"
    assert calls[0]["data"] == {"적재 공간": ["트렁크"], "쌓는 짐이 있음": False}
    assert all("운반 상자" in step["text"] for step in response.json()["data"]["steps"])


@pytest.mark.parametrize("kind", ["saved", "preview"])
def test_guide_keeps_historical_vehicle_geometry_after_catalog_update(guide_app, kind):
    app = guide_app
    legacy = VehicleDetail.model_validate_json(
        files("app.data.vehicles").joinpath("avante-demo-v2.json").read_text(encoding="utf-8")
    )
    app.monkeypatch.setattr(
        "app.services.calculations.get_vehicle_catalog", lambda: {legacy.vehicle_id: legacy}
    )
    reference, calculation, _ = source(app, kind)
    assert calculation["vehicle_data_version"] == "avante-demo-v2"
    install_model(app)
    before = post(app.client, PATH, reference)
    assert before.status_code == 200, before.text
    current = get_vehicle_catalog()[legacy.vehicle_id].model_dump(mode="json")
    current["data_version"] = "avante-demo-v3"
    for profile in current["packing_profiles"]:
        for space in profile["spaces"]:
            space["origin_cm"]["x_cm"] += 500
    updated = VehicleDetail.model_validate(current)
    app.monkeypatch.setattr(
        "app.services.calculations.get_vehicle_catalog", lambda: {updated.vehicle_id: updated}
    )
    response = post(app.client, PATH, reference)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["source"] == "AI"
    assert response.json() == before.json()


def test_guide_accepts_requested_exclusion_alternative(guide_app):
    app = guide_app
    reference, calculation, candidate = source(app, "preview")
    # 선택한 후보가 alternatives에 있을 때도 같은 낱개 배치를 사용해야 합니다.
    with app.session.begin():
        row = app.session.get(CalculationPreview, UUID(reference["preview_id"]))
        value = deepcopy(calculation)
        value["arrangements"] = []
        value["alternatives"] = [candidate]
        value["result_code"] = "EXCLUSION_OPTIONS_FOUND"
        value["is_success"] = False
        row.result_payload = value
    install_model(app)
    response = post(app.client, PATH, reference)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["arrangement_id"] == reference["arrangement_id"]


@pytest.mark.parametrize(
    ("change", "expected_code"),
    [
        ("missing_plan", "PLAN_NOT_FOUND"),
        ("other_owner", "PLAN_NOT_FOUND"),
        ("missing_calculation", "CALCULATION_NOT_FOUND"),
        ("other_plan_calculation", "CALCULATION_NOT_FOUND"),
        ("missing_arrangement", "ARRANGEMENT_NOT_FOUND"),
    ],
)
def test_saved_guide_reference_must_belong_to_current_user_and_plan(
    guide_app, change, expected_code
):
    app = guide_app
    reference, _, _ = source(app)
    if change == "missing_plan":
        reference["plan_id"] = str(uuid4())
    elif change == "missing_calculation":
        reference["calculation_id"] = str(uuid4())
    elif change == "missing_arrangement":
        reference["arrangement_id"] = str(uuid4())
    elif change == "other_plan_calculation":
        other, _, _ = source(app)
        reference["calculation_id"] = other["calculation_id"]
    else:
        with app.session.begin():
            app.session.get(Plan, UUID(reference["plan_id"])).user_id = "other-user"
    calls = install_model(app)
    response = post(app.client, PATH, reference)
    assert response.status_code == 404
    assert response.json()["code"] == expected_code
    assert calls == []


@pytest.mark.parametrize("missing", [False, True])
def test_preview_ownership_is_checked_before_model(guide_app, missing):
    app = guide_app
    reference, _, _ = source(app, "preview")
    if missing:
        reference["preview_id"] = str(uuid4())
    else:
        with app.session.begin():
            app.session.get(
                CalculationPreview, UUID(reference["preview_id"])
            ).user_id = "other-user"
    calls = install_model(app)
    response = post(app.client, PATH, reference)
    assert response.status_code == 404
    assert response.json()["code"] == "PREVIEW_NOT_FOUND"
    assert calls == []


@pytest.mark.parametrize("already_generated", [False, True])
def test_expired_preview_is_rejected_even_for_cached_guide(guide_app, already_generated):
    app = guide_app
    reference, _, _ = source(app, "preview")
    calls = install_model(app)
    key = uuid4()
    if already_generated:
        assert post(app.client, PATH, reference, key).status_code == 200
    with app.session.begin():
        preview = app.session.get(CalculationPreview, UUID(reference["preview_id"]))
        preview.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    response = post(app.client, PATH, reference, key)
    assert response.status_code == 410
    assert response.json()["code"] == "PREVIEW_EXPIRED"
    assert len(calls) == int(already_generated)


def test_same_key_replays_without_model_and_other_reference_is_rejected(guide_app):
    app = guide_app
    reference, _, _ = source(app)
    other, _, _ = source(app, "preview")
    calls = install_model(app)
    key = uuid4()
    first = post(app.client, PATH, reference, key)
    repeated = post(app.client, PATH, reference, key)
    assert first.status_code == repeated.status_code == 200
    assert first.json() == repeated.json()
    conflict = post(app.client, PATH, other, key)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert len(calls) == 1


def test_cached_guide_does_not_bypass_current_plan_ownership(guide_app):
    app = guide_app
    reference, _, _ = source(app)
    calls = install_model(app)
    key = uuid4()
    assert post(app.client, PATH, reference, key).status_code == 200
    with app.session.begin():
        app.session.get(Plan, UUID(reference["plan_id"])).user_id = "other-user"
    response = post(app.client, PATH, reference, key)
    assert response.status_code == 404
    assert response.json()["code"] == "PLAN_NOT_FOUND"
    assert len(calls) == 1


def test_preview_expiring_during_model_call_does_not_save_a_guide(guide_app):
    app = guide_app
    reference, _, _ = source(app, "preview")

    def expire():
        assert not app.session.in_transaction()
        with app.session.begin():
            row = app.session.get(CalculationPreview, UUID(reference["preview_id"]))
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    install_model(app, side_effect=expire)
    key = uuid4()
    response = post(app.client, PATH, reference, key)
    assert response.status_code == 410
    assert response.json()["code"] == "PREVIEW_EXPIRED"
    with app.session.begin():
        assert (
            app.session.scalar(select(OperationReceipt).where(OperationReceipt.key == key)) is None
        )


@pytest.mark.parametrize("failure", [LlmUnavailable("provider detail"), TimeoutError("timeout")])
def test_model_failure_returns_fallback_without_exposing_details(guide_app, failure):
    app = guide_app
    reference, _, _ = source(app)

    def fail():
        raise failure

    calls = install_model(app, side_effect=fail)
    key = uuid4()
    first = post(app.client, PATH, reference, key)
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert (data["source"], data["reply"], data["steps"]) == ("FALLBACK", FALLBACK, [])
    assert "provider detail" not in first.text
    assert post(app.client, PATH, reference, key).json() == first.json()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "reply",
    [
        "그림 좌표를 맞춰 주세요.",
        "최종 폴딩 상태를 확인해 주세요.",
        "트렁크(TRUNK)에 놓아 주세요.",
        "높이 10cm에 놓아 주세요.",
        "이 상자는 500kg까지 견딜 수 있습니다.",
        "어떤 속도로 운전해도 안전을 보장합니다.",
        "짐을 오른쪽에 세워 주세요.",
    ],
)
def test_guide_falls_back_on_technical_or_unverified_summary(guide_app, reply):
    app = guide_app
    reference, _, _ = source(app)
    install_model(app, reply=reply)
    response = post(app.client, PATH, reference)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["source"], data["reply"], data["steps"]) == ("FALLBACK", FALLBACK, [])


def test_legacy_calculation_without_snapshot_does_not_invent_guide(guide_app):
    app = guide_app
    reference, _, _ = source(app)
    with app.session.begin():
        app.session.get(Calculation, UUID(reference["calculation_id"])).result_payload = None
    calls = install_model(app)
    response = post(app.client, PATH, reference)
    assert response.status_code == 503
    assert response.json()["code"] == "CALCULATION_RESULT_UNAVAILABLE"
    assert calls == []


@pytest.mark.parametrize("support", ["missing", "later", "self"])
def test_corrupt_support_relationship_is_rejected_before_model(guide_app, support):
    app = guide_app
    reference, calculation, _ = source(app)
    value = deepcopy(calculation)
    placements = value["arrangements"][0]["placements"]
    first, second = sorted(placements, key=lambda p: p["load_order"])
    first["supporting_placement_ids"] = [
        str(uuid4())
        if support == "missing"
        else second["placement_id"]
        if support == "later"
        else first["placement_id"]
    ]
    with app.session.begin():
        app.session.get(Calculation, UUID(reference["calculation_id"])).result_payload = value
    calls = install_model(app)
    response = post(app.client, PATH, reference)
    assert response.status_code == 503
    assert response.json()["code"] == "CALCULATION_RESULT_UNAVAILABLE"
    assert calls == []


@pytest.mark.parametrize("kind", ["saved", "preview"])
@pytest.mark.parametrize("damage", ["outside_space", "cross_space_support"])
def test_guide_rejects_geometry_outside_its_saved_space(guide_app, kind, damage):
    app = guide_app
    reference, calculation, _ = source(app, kind)
    value = deepcopy(calculation)
    arrangement = value["arrangements"][0]
    first, second = sorted(arrangement["placements"], key=lambda p: p["load_order"])
    original_space = next(
        space for space in arrangement["packing_spaces"] if space["space_id"] == first["space_id"]
    )
    if damage == "outside_space":
        first["y_cm"] = original_space["depth_cm"]
    else:
        adjacent_space = deepcopy(original_space)
        adjacent_space["space_id"] = "ADJACENT_SPACE"
        adjacent_space["origin_cm"]["y_cm"] += original_space["depth_cm"]
        arrangement["packing_spaces"].append(adjacent_space)
        second.update(
            space_id=adjacent_space["space_id"],
            x_cm=first["x_cm"],
            y_cm=first["y_cm"],
            z_cm=10,
            supporting_placement_ids=[first["placement_id"]],
        )
        arrangement["stacking_assessments"] = [
            {
                "lower_item_id": first["item_id"],
                "upper_item_id": second["item_id"],
                "allowed": True,
                "basis": "LLM_ESTIMATE",
                "reason": "모의 쌓기 추정",
            }
        ]
    with app.session.begin():
        row = (
            app.session.get(Calculation, UUID(reference["calculation_id"]))
            if kind == "saved"
            else app.session.get(CalculationPreview, UUID(reference["preview_id"]))
        )
        row.result_payload = value
    calls = install_model(app)
    response = post(app.client, PATH, reference)
    assert response.status_code == 503
    assert response.json()["code"] == "CALCULATION_RESULT_UNAVAILABLE"
    assert calls == []


def test_source_changing_during_generation_does_not_save_old_guide(guide_app):
    app = guide_app
    reference, calculation, _ = source(app, "preview")

    def change_source():
        with app.session.begin():
            row = app.session.get(CalculationPreview, UUID(reference["preview_id"]))
            value = deepcopy(calculation)
            value["arrangements"][0]["packing_spaces"][0]["origin_cm"]["x_cm"] += 1
            row.result_payload = value

    install_model(app, side_effect=change_source)
    key = uuid4()
    response = post(app.client, PATH, reference, key)
    assert response.status_code == 409
    assert response.json()["code"] == "GUIDE_SOURCE_CHANGED"
    with app.session.begin():
        assert (
            app.session.scalar(select(OperationReceipt).where(OperationReceipt.key == key)) is None
        )


@pytest.mark.parametrize("kind", ["saved", "preview"])
def test_camping_guide_describes_the_saved_position_and_rotation_in_korean(guide_app, kind):
    app = guide_app
    plan = {
        **PLAN,
        "items": [
            {
                "name": "캠핑 웨건",
                "width_cm": 90,
                "depth_cm": 50,
                "height_cm": 30,
                "quantity": 1,
                "allowed_orientations": ["WDH"],
            },
            {
                "name": "아이스박스",
                "width_cm": 60,
                "depth_cm": 40,
                "height_cm": 42,
                "quantity": 1,
                "allowed_orientations": ["WHD"],
            },
        ],
    }
    reference, _, candidate = source(app, kind, plan)
    placements = sorted(candidate["placements"], key=lambda p: p["load_order"])
    assert [
        (p["space_id"], p["x_cm"], p["y_cm"], p["z_cm"], p["orientation"]) for p in placements
    ] == [("TRUNK", 0, 0, 0, "WDH"), ("TRUNK", 0, 50, 0, "WHD")]
    install_model(app, reply="그림에 나온 순서대로 트렁크에 짐을 실어 주세요.")
    response = post(app.client, PATH, reference)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["source"] == "AI"
    assert data["reply"] == "그림에 나온 순서대로 트렁크에 짐을 실어 주세요."
    assert data["steps"] == [
        {
            "order": 1,
            "placement_id": placements[0]["placement_id"],
            "text": "캠핑 웨건을 트렁크 입구 쪽 왼쪽 바닥에 긴 쪽이 좌우로 향하게 놓아 주세요.",
        },
        {
            "order": 2,
            "placement_id": placements[1]["placement_id"],
            "text": (
                "아이스박스를 트렁크 안쪽 왼쪽 바닥에 옆으로 눕혀 "
                "긴 쪽이 좌우로 향하게 놓아 주세요."
            ),
        },
    ]


def test_guide_names_the_supporting_item_before_the_stacked_item(guide_app):
    app = guide_app

    def allow_stacking(items, settings):
        return [
            {
                "lower_item_id": items[0]["item_id"],
                "upper_item_id": items[1]["item_id"],
                "allowed": True,
                "basis": "LLM_ESTIMATE",
                "reason": "테스트용 쌓기 판단",
            }
        ]

    app.monkeypatch.setattr("app.services.llm.estimate_stacking", allow_stacking)
    plan = {
        **PLAN,
        "items": [
            {
                "name": "나무 상자",
                "width_cm": 90,
                "depth_cm": 65,
                "height_cm": 20,
                "quantity": 1,
                "allowed_orientations": ["WDH"],
            },
            {
                "name": "캠핑 가방",
                "width_cm": 60,
                "depth_cm": 55,
                "height_cm": 20,
                "quantity": 1,
                "allowed_orientations": ["WDH"],
            },
        ],
    }
    reference, _, candidate = source(app, "preview", plan)
    lower, upper = sorted(candidate["placements"], key=lambda p: p["load_order"])
    assert upper["supporting_placement_ids"] == [lower["placement_id"]]
    calls = install_model(app)
    response = post(app.client, PATH, reference)
    assert response.status_code == 200, response.text
    first, second = response.json()["data"]["steps"]
    assert first["placement_id"] == lower["placement_id"]
    assert second["placement_id"] == upper["placement_id"]
    assert "나무 상자 위에" in second["text"]
    assert calls[0]["data"]["쌓는 짐이 있음"]


def test_concurrent_guide_request_cannot_duplicate_model_call(guide_app, settings):
    app = guide_app
    reference, _, _ = source(app)
    calls = install_model(app)
    key = uuid4()
    with request_guard(app.session, settings.demo_user_id, "/ai/loading-guides", key):
        response = post(app.client, PATH, reference, key)
    assert response.status_code == 409
    assert response.json()["code"] == "REQUEST_IN_PROGRESS"
    assert calls == []
    assert post(app.client, PATH, reference, key).status_code == 200
    assert len(calls) == 1


def test_keep_existing_guide_preserves_loaded_items_and_only_adds_new_cargo(guide_app):
    app = guide_app
    reference, calculation, candidate = source(app)
    response = app.client.put(
        f"/api/v1/plans/{reference['plan_id']}/selection",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "expected_revision": 1,
            "calculation_id": reference["calculation_id"],
            "arrangement_id": reference["arrangement_id"],
        },
    )
    assert response.status_code == 200, response.text
    proposed = deepcopy(calculation["input_snapshot"])
    proposed.pop("vehicle_state", None)
    proposed["items"].append(
        {"name": "추가 상자", "width_cm": 5, "depth_cm": 5, "height_cm": 5, "quantity": 1}
    )
    response = post(
        app.client,
        "/api/v1/calculations/preview",
        {
            "source_plan_id": reference["plan_id"],
            "expected_revision": 1,
            "plan": proposed,
            "calculation_options": {
                "packing_mode": "KEEP_EXISTING",
                "cargo_state": "SELECTED_ARRANGEMENT",
                "base_calculation_id": reference["calculation_id"],
                "base_arrangement_id": reference["arrangement_id"],
            },
        },
    )
    assert response.status_code == 201, response.text
    preview = response.json()["data"]
    assert preview["calculation"]["result_code"] == "LAYOUT_FOUND"
    kept = preview["calculation"]["arrangements"][0]
    fixed = {p["placement_id"] for p in candidate["placements"]}
    assert len(kept["placements"]) == len(fixed) + 1
    for placement in candidate["placements"]:
        assert placement in kept["placements"]
    install_model(app)
    response = post(
        app.client,
        PATH,
        {"preview_id": preview["preview_id"], "arrangement_id": kept["arrangement_id"]},
    )
    assert response.status_code == 200, response.text
    guide = response.json()["data"]
    assert guide["source"] == "AI"
    assert "기존 짐" in guide["reply"] and "새 짐만 추가" in guide["reply"]
    for step in guide["steps"]:
        if step["placement_id"] in fixed:
            assert "이미 실은 운반 상자" in step["text"]
            assert "현재 위치와 방향 그대로 두세요." in step["text"]
        else:
            assert "추가 상자" in step["text"]
