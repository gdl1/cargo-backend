from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.db.seed import seed_vehicles
from app.db.session import get_db
from app.schemas.packing import PackingProblem
from app.services.llm import ResponsesClient
from app.services.packing import SearchBudget, find_layout, pack, validate_packing
from app.services.recommendations import recommend
from app.services.vehicles import get_vehicle_catalog

OCCUPIED_FRONT = {"current_folded_segments": [], "occupied_seats": ["ROW_1_LEFT", "ROW_1_RIGHT"]}


@pytest.fixture
def tucson() -> dict:
    return get_vehicle_catalog()["tucson_hybrid"].model_dump(mode="json")


def cargo(width=40, depth=140, height=20) -> dict:
    return {
        "name": "긴 상자",
        "width_cm": width,
        "depth_cm": depth,
        "height_cm": height,
        "quantity": 1,
        "allowed_orientations": ["WDH"],
    }


def problem(tucson, *items) -> PackingProblem:
    return PackingProblem.model_validate(
        {"spaces": tucson["packing_profiles"][1]["spaces"], "items": items}
    )


def test_long_cargo_uses_both_spaces_with_one_placement(tucson):
    plan = {
        "vehicle_id": tucson["vehicle_id"],
        "passenger_count": 2,
        "items": [dict(cargo(), item_id="long")],
    }
    result = recommend(plan, tucson, OCCUPIED_FRONT, stacking_assessments=[])
    assert result["result_code"] == "LAYOUT_FOUND"
    (candidate,) = result["arrangements"]
    assert candidate["target_folded_segments"] == ["REAR_ALL"]
    assert candidate["all_in_trunk"] is False
    trunk, rear = candidate["packing_spaces"]
    assert (trunk["width_cm"], trunk["depth_cm"], trunk["height_cm"]) == (123, 100, 80)
    assert trunk["opening"] == {"width_cm": 116, "height_cm": 80}
    assert rear["origin_cm"]["y_cm"] + rear["depth_cm"] == 160
    assert rear["height_cm"] == 70
    (placement,) = candidate["placements"]
    assert (placement["space_id"], placement["spanning_space_id"]) == ("TRUNK", "REAR_FOLDED")
    assert (placement["x_cm"], placement["y_cm"], placement["z_cm"]) == (3.5, 0, 0)


@pytest.mark.parametrize(
    ("plan_constraints", "options", "expected"),
    [
        ({"car_seats": ["ROW_2_RIGHT"]}, OCCUPIED_FRONT, "NO_LAYOUT_FOUND"),
        ({"retained_seats": ["ROW_2_LEFT"]}, OCCUPIED_FRONT, "NO_LAYOUT_FOUND"),
        ({"passenger_count": 3}, OCCUPIED_FRONT, "NO_LAYOUT_FOUND"),
        ({}, {}, "STATE_REQUIRED"),
    ],
)
def test_connected_layout_respects_seats(tucson, plan_constraints, options, expected):
    plan = {
        "vehicle_id": tucson["vehicle_id"],
        "passenger_count": 2,
        "items": [dict(cargo(), item_id="long")],
        **plan_constraints,
    }
    result = recommend(plan, tucson, options, stacking_assessments=[])
    assert result["result_code"] == expected
    if expected == "STATE_REQUIRED":
        assert {row["seat_code"] for row in result["required_inputs"]} == {
            "ROW_2_LEFT",
            "ROW_2_CENTER",
            "ROW_2_RIGHT",
        }


@pytest.mark.parametrize(
    ("dimensions", "fits"),
    [
        ((116, 160, 70), True),
        ((116.1, 160, 70), False),
        ((116, 160.1, 70), False),
        ((116, 160, 70.1), False),
    ],
)
def test_connected_layout_respects_width_length_and_height(tucson, dimensions, fits):
    result = pack(problem(tucson, dict(cargo(*dimensions), item_id="long")))
    assert result.result_code == ("LAYOUT_FOUND" if fits else "NO_LAYOUT_FOUND")


def test_step_prevents_level_connection(tucson):
    tucson["packing_profiles"][1]["spaces"][1]["origin_cm"]["z_cm"] = 10
    with pytest.raises(ValidationError, match="바닥 높이가 같고"):
        problem(tucson, dict(cargo(), item_id="long"))


@pytest.mark.parametrize(("space_id", "x", "y"), [("TRUNK", 3.5, 50), ("REAR_FOLDED", 0, 10)])
def test_connected_cargo_collides_with_boxes_in_either_space(tucson, space_id, x, y):
    long = dict(cargo(), item_id="long")
    small = dict(cargo(10, 10, 10), item_id="small")
    (spanning,) = pack(problem(tucson, long)).placements
    other = {
        "placement_id": "small-box",
        "item_id": "small",
        "unit_index": 1,
        "space_id": space_id,
        "x_cm": x,
        "y_cm": y,
        "z_cm": 0,
        "orientation": "WDH",
        "load_order": 2,
    }
    both = problem(tucson, long, small)
    with pytest.raises(ValueError, match="짐이 겹칩니다"):
        validate_packing(both, [spanning, other])
    extra = find_layout(both, budget=SearchBudget.create(1), fixed_placements=[spanning])
    assert extra and extra[0] == spanning and len(extra) == 2


@pytest.mark.integration
def test_connected_layout_survives_preview_save_guide_and_recalculation(
    application, model_session, monkeypatch
):
    seed_vehicles(model_session)
    model_session.commit()
    application.dependency_overrides[get_db] = lambda: model_session

    def generate(client, name, schema, instructions, context):
        assert context["적재 공간"] == ["접힌 뒷좌석", "트렁크"]
        return schema.model_validate({"reply": "그림의 순서대로 짐을 놓아 주세요."})

    monkeypatch.setattr(ResponsesClient, "call_function", generate)

    with TestClient(application) as client:

        def post(path, payload, status=201):
            response = client.post(path, json=payload, headers={"Idempotency-Key": str(uuid4())})
            assert response.status_code == status, response.text
            return response.json()["data"]

        preview = post(
            "/api/v1/calculations/preview",
            {
                "plan": {"vehicle_id": "tucson_hybrid", "passenger_count": 2, "items": [cargo()]},
                "calculation_options": OCCUPIED_FRONT,
            },
        )
        (candidate,) = preview["calculation"]["arrangements"]
        guide = post(
            "/api/v1/ai/loading-guides",
            {"preview_id": preview["preview_id"], "arrangement_id": candidate["arrangement_id"]},
            status=200,
        )
        assert "트렁크에서 접힌 뒷좌석까지 걸치도록" in guide["steps"][0]["text"]
        assert "REAR_FOLDED" not in guide["steps"][0]["text"]
        assert "좌석은 그림에 나온 상태인지 확인해 주세요." in guide["reply"]
        saved = post(
            "/api/v1/plans",
            {
                "source_preview_id": preview["preview_id"],
                "selected_arrangement_id": candidate["arrangement_id"],
            },
        )
        path = f"/api/v1/plans/{saved['plan_id']}/calculations"
        response = client.get(f"{path}/{saved['selected_calculation_id']}")
        assert response.status_code == 200, response.text
        (saved_placement,) = response.json()["data"]["arrangements"][0]["placements"]
        assert saved_placement["spanning_space_id"] == "REAR_FOLDED"
        model_session.rollback()
        kept = post(
            path,
            {
                **OCCUPIED_FRONT,
                "current_folded_segments": ["REAR_ALL"],
                "packing_mode": "KEEP_EXISTING",
                "cargo_state": "SELECTED_ARRANGEMENT",
                "base_calculation_id": saved["selected_calculation_id"],
                "base_arrangement_id": saved["selected_arrangement_id"],
            },
        )
        (kept_placement,) = kept["arrangements"][0]["placements"]
        assert kept_placement["spanning_space_id"] == "REAR_FOLDED"
        assert kept_placement["placement_id"] == saved_placement["placement_id"]
        assert all(
            kept_placement[axis] == saved_placement[axis] for axis in ("x_cm", "y_cm", "z_cm")
        )
