import json
from copy import deepcopy
from itertools import combinations
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db.seed import seed_vehicles
from app.db.session import get_db
from app.schemas.packing import PackingProblem
from app.schemas.vehicle import VehicleDetail, spaces_overlap
from app.services.packing import pack
from app.services.vehicles import get_vehicle_catalog


def load_vehicle(version: int) -> VehicleDetail:
    path = Path(__file__).parents[1] / f"app/data/vehicles/avante-demo-v{version}.json"
    return VehicleDetail.model_validate(json.loads(path.read_text(encoding="utf-8")))


def test_v3_spaces_touch_without_overlapping_in_every_fold_combination() -> None:
    vehicle = get_vehicle_catalog()["avante_demo"]
    assert vehicle.data_version == "avante-demo-v3"
    assert vehicle.data_source == "DUMMY"
    assert len(vehicle.packing_profiles) == 8
    for profile in vehicle.packing_profiles:
        trunk = next(space for space in profile.spaces if space.kind == "TRUNK")
        rear = [space for space in profile.spaces if space.space_id.startswith("REAR_")]
        front = next(space for space in profile.spaces if space.space_id.startswith("FRONT_"))
        assert trunk.depth_cm == 110 and trunk.height_cm == 50
        assert all(space.origin_cm.y_cm == trunk.origin_cm.y_cm + trunk.depth_cm for space in rear)
        assert front.origin_cm.y_cm == 173
        assert all(space.origin_cm.y_cm + space.depth_cm <= front.origin_cm.y_cm for space in rear)
        assert all(
            not spaces_overlap(first, second) for first, second in combinations(profile.spaces, 2)
        )
        assert all(space.data_source == "DUMMY" for space in profile.spaces)


def test_only_selected_reference_dimensions_change_from_v2() -> None:
    old = load_vehicle(2).model_dump(mode="json")
    current = get_vehicle_catalog()["avante_demo"].model_dump(mode="json")
    assert old["data_version"] == "avante-demo-v2"
    assert old["opening"] == current["opening"] == {"width_cm": 99.0, "height_cm": 41.5}
    assert old["fold_segments"] == current["fold_segments"]
    for before, after in zip(old["packing_profiles"], current["packing_profiles"], strict=True):
        expected = deepcopy(before)
        for space in expected["spaces"]:
            if space["kind"] == "TRUNK":
                assert (space["width_cm"], space["depth_cm"], space["height_cm"]) == (97, 82, 42)
                space.update(depth_cm=110.0, height_cm=50.0)
            elif space["space_id"].startswith("REAR_"):
                space["origin_cm"]["y_cm"] = 110.0
            else:
                space["origin_cm"]["y_cm"] = 173.0
                if space["kind"] == "SEAT":
                    space["width_cm"] = 48.0
        assert after == expected


def test_trunk_dimension_change_without_origin_update_is_rejected() -> None:
    vehicle = load_vehicle(2).model_dump(mode="json")
    for profile in vehicle["packing_profiles"]:
        for space in profile["spaces"]:
            if space["kind"] == "TRUNK":
                space["depth_cm"] = 110
    with pytest.raises(ValidationError, match="겹칠"):
        VehicleDetail.model_validate(vehicle)


def test_larger_trunk_height_does_not_remove_opening_limit() -> None:
    vehicle = get_vehicle_catalog()["avante_demo"]
    trunk = next(space for space in vehicle.packing_profiles[0].spaces if space.kind == "TRUNK")
    problem = PackingProblem.model_validate(
        {
            "spaces": [trunk.model_dump(mode="json")],
            "items": [
                {
                    "item_id": "box",
                    "width_cm": 20,
                    "depth_cm": 20,
                    "height_cm": 45,
                    "quantity": 1,
                    "allowed_orientations": ["WDH"],
                }
            ],
        }
    )
    assert trunk.height_cm == 50
    assert pack(problem).result_code == "NO_LAYOUT_FOUND"


@pytest.mark.integration
def test_v2_result_keeps_its_geometry_after_v3_update(
    application: FastAPI, model_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_vehicle = load_vehicle(2)
    current_vehicle = get_vehicle_catalog()["avante_demo"]
    monkeypatch.setattr("app.services.vehicles._load_vehicle_data", lambda: (old_vehicle,))
    seed_vehicles(model_session)
    model_session.commit()
    application.dependency_overrides[get_db] = lambda: model_session

    def headers() -> dict[str, str]:
        return {"Idempotency-Key": str(uuid4())}

    options = {"occupied_seats": ["ROW_1_LEFT"], "current_folded_segments": []}
    with TestClient(application) as client:
        created = client.post(
            "/api/v1/plans",
            json={
                "vehicle_id": "avante_demo",
                "passenger_count": 1,
                "items": [
                    {
                        "name": "상자",
                        "width_cm": 60,
                        "depth_cm": 60,
                        "height_cm": 40,
                        "quantity": 2,
                        "allowed_orientations": ["WDH"],
                    }
                ],
            },
            headers=headers(),
        )
        assert created.status_code == 201, created.text
        plan_id = created.json()["data"]["plan_id"]
        path = f"/api/v1/plans/{plan_id}"
        old_response = client.post(f"{path}/calculations", json=options, headers=headers())
        assert old_response.status_code == 201, old_response.text
        old = old_response.json()["data"]
        assert old["result_code"] == "LAYOUT_FOUND"
        arrangement = old["arrangements"][0]
        assert {row["space_id"] for row in arrangement["placements"]} == {
            "TRUNK",
            "REAR_LEFT_FOLDED",
        }
        selected = client.put(
            f"{path}/selection",
            json={
                "expected_revision": 1,
                "calculation_id": old["calculation_id"],
                "arrangement_id": arrangement["arrangement_id"],
            },
            headers=headers(),
        )
        assert selected.status_code == 200, selected.text

        monkeypatch.setattr("app.services.vehicles._load_vehicle_data", lambda: (current_vehicle,))
        model_response = client.get("/api/v1/vehicles/avante_demo")
        assert model_response.status_code == 200
        assert model_response.json()["data"] == current_vehicle.model_dump(mode="json")
        read_old = client.get(f"{path}/calculations/{old['calculation_id']}")
        assert read_old.status_code == 200
        assert read_old.json()["data"] == old
        model_session.rollback()

        new_response = client.post(f"{path}/calculations", json=options, headers=headers())
        assert new_response.status_code == 201, new_response.text
        current = new_response.json()["data"]
        assert current["vehicle_data_version"] == "avante-demo-v3"
        assert old["vehicle_data_version"] == "avante-demo-v2"
        profiles = {row.profile_id: row for row in current_vehicle.packing_profiles}
        for candidate in current["arrangements"]:
            source = {
                space.space_id: space.model_dump(mode="json")
                for space in profiles[candidate["profile_id"]].spaces
            }
            for snapshot in candidate["packing_spaces"]:
                expected = source[snapshot["space_id"]]
                for field in ("width_cm", "depth_cm", "height_cm", "origin_cm"):
                    assert snapshot[field] == expected[field]

        stale = client.post(
            f"{path}/calculations",
            json={
                **options,
                "packing_mode": "KEEP_EXISTING",
                "cargo_state": "SELECTED_ARRANGEMENT",
                "base_calculation_id": old["calculation_id"],
                "base_arrangement_id": arrangement["arrangement_id"],
            },
            headers=headers(),
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["code"] == "BASE_LAYOUT_STALE"
        assert client.get(f"{path}/calculations/{old['calculation_id']}").json()["data"] == old
        assert client.get(path).json()["data"]["selected_calculation_id"] == old["calculation_id"]
