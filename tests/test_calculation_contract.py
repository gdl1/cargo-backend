from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.exceptions import AppError
from app.schemas.calculation import CalculationCreate, PreviewCreate
from app.services.calculations import run_calculation, vehicle_snapshot

ITEM = {
    "item_id": str(uuid4()),
    "name": "상자",
    "width_cm": 10,
    "depth_cm": 10,
    "height_cm": 10,
    "quantity": 1,
    "required_quantity": 0,
    "allowed_orientations": ["WDH"],
    "transport_state": "UNKNOWN",
    "material": None,
}
PLAN = {"vehicle_id": "avante_demo", "passenger_count": 1, "items": [ITEM]}


def test_null_empty_and_unknown_are_distinct():
    assert CalculationCreate().current_folded_segments is None
    assert CalculationCreate(current_folded_segments=[]).current_folded_segments == []
    assert CalculationCreate(cargo_state="UNKNOWN").cargo_state == "UNKNOWN"


@pytest.mark.parametrize(
    "payload",
    [
        {"packing_mode": "KEEP_EXISTING"},
        {"cargo_state": "SELECTED_ARRANGEMENT"},
        {"base_calculation_id": str(uuid4())},
        {"occupied_seats": ["ROW_1_LEFT", "ROW_1_LEFT"]},
        {"expected_revision": True},
        {"allow_exclusions": "false"},
    ],
)
def test_invalid_options_are_rejected(payload):
    with pytest.raises(ValidationError):
        CalculationCreate.model_validate(payload)


def test_preview_requires_source_revision_and_preserves_item_ids():
    data = PreviewCreate.model_validate({"plan": PLAN})
    assert str(data.plan.items[0].item_id) == ITEM["item_id"]
    with pytest.raises(ValidationError):
        PreviewCreate.model_validate({"plan": PLAN, "source_plan_id": str(uuid4())})


def test_unknown_cargo_does_not_call_ai(monkeypatch, settings):
    def forbidden(*args, **kwargs):
        pytest.fail("상태 보완 응답에 외부 호출이나 배치 탐색이 필요하지 않습니다.")

    monkeypatch.setattr("app.services.llm.estimate_stacking", forbidden)
    result = run_calculation(
        PLAN, vehicle_snapshot("avante_demo"), CalculationCreate(cargo_state="UNKNOWN"), settings
    )
    assert result["result_code"] == "STATE_REQUIRED"
    assert result["arrangements"] == []


def test_quantity_limit_is_checked_before_ai(monkeypatch, settings):
    def forbidden(*args, **kwargs):
        pytest.fail("큰 입력은 외부 호출 전에 거절해야 합니다.")

    monkeypatch.setattr("app.services.llm.estimate_stacking", forbidden)
    plan = deepcopy(PLAN)
    plan["items"][0]["quantity"] = 65
    with pytest.raises(AppError) as error:
        run_calculation(plan, {}, CalculationCreate(), settings)
    assert error.value.code == "VALIDATION_ERROR"


def test_timeout_never_leaks_found_candidates(monkeypatch, settings):
    monkeypatch.setattr(
        "app.services.recommendations.recommend",
        lambda *args, **kwargs: {
            "result_code": "TIME_LIMIT",
            "is_success": True,
            "search_complete": True,
            "arrangements": [{"rank": 1}],
            "alternatives": [{"rank": 2}],
        },
    )
    result = run_calculation(
        PLAN,
        vehicle_snapshot("avante_demo"),
        CalculationCreate(),
        settings,
        stacking_assessments=[],
    )
    assert result["arrangements"] == result["alternatives"] == []
    assert not result["is_success"] and not result["search_complete"]


def test_changed_item_does_not_reuse_stacking_estimate(monkeypatch, settings):
    from app.services.llm import StackingUnavailable

    plan = deepcopy(PLAN)
    plan["items"][0]["quantity"] = 2
    base = {
        "input_snapshot": deepcopy(plan),
        "stacking_assessments": [
            {
                "lower_item_id": ITEM["item_id"],
                "upper_item_id": ITEM["item_id"],
                "allowed": True,
                "basis": "LLM_ESTIMATE",
                "reason": "상자 조합 추정",
            }
        ],
    }
    plan["items"][0]["material"] = "유리"

    def unavailable(*args, **kwargs):
        raise StackingUnavailable("unavailable")

    captured = {}

    def recommend(*args, **kwargs):
        captured.update(kwargs)
        return {"result_code": "NO_LAYOUT_FOUND", "is_success": False, "search_complete": True}

    monkeypatch.setattr("app.services.llm.estimate_stacking", unavailable)
    monkeypatch.setattr("app.services.recommendations.recommend", recommend)
    result = run_calculation(
        plan, vehicle_snapshot("avante_demo"), CalculationCreate(), settings, base_arrangement=base
    )
    assert captured["stacking_assessments"] == []
    assert "STACKING_ASSESSMENT_UNAVAILABLE" in result["warnings"]


def test_vehicle_observations_require_timezone_and_unique_seats():
    base = {
        "observed_at": datetime.now(UTC).isoformat(),
        "seats": [{"seat_code": "ROW_1_LEFT", "occupancy": "OCCUPIED", "source": "SDK"}],
    }
    assert CalculationCreate(vehicle_state=base).vehicle_state.seats[0].source == "SDK"
    base["seats"] *= 2
    with pytest.raises(ValidationError):
        CalculationCreate(vehicle_state=base)


def test_openapi_exposes_placement_and_space_fields(application):
    schemas = application.openapi()["components"]["schemas"]
    assert {"space_id", "unit_index", "supporting_placement_ids", "x_cm", "orientation"} <= set(
        schemas["PlacementData"]["properties"]
    )
    assert "packing_spaces" in schemas["ArrangementData"]["properties"]
    assert "exclusions" in schemas["ArrangementData"]["properties"]
