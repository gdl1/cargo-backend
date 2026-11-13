import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.vehicle import VehicleDetail
from app.services.vehicles import get_vehicle_catalog


@pytest.fixture
def space_vehicle() -> dict[str, Any]:
    trunk = {
        "space_id": "TRUNK",
        "kind": "TRUNK",
        "width_cm": 100,
        "depth_cm": 80,
        "height_cm": 40,
        "origin_cm": {"x_cm": 0, "y_cm": 0, "z_cm": 0},
        "seat_codes": [],
        "opening": None,
        "data_source": "DUMMY",
    }
    seat = {
        **trunk,
        "space_id": "FRONT_RIGHT_SEAT",
        "kind": "SEAT",
        "width_cm": 40,
        "depth_cm": 40,
        "origin_cm": {"x_cm": 0, "y_cm": 80, "z_cm": 0},
        "seat_codes": ["ROW_1_RIGHT"],
    }
    return {
        "vehicle_id": "test",
        "name": "검증용 차량",
        "seat_capacity": 2,
        "data_version": "test-v1",
        "data_source": "DUMMY",
        "seat_codes": ["ROW_1_LEFT", "ROW_1_RIGHT"],
        "driver_seat": "ROW_1_LEFT",
        "fold_segments": [{"code": "FRONT", "seat_codes": ["ROW_1_RIGHT"]}],
        "packing_profiles": [
            {
                "profile_id": "BASE",
                "target_folded_segments": [],
                "spaces": [trunk, seat],
                "packing_space": None,
            }
        ],
        "opening": None,
    }


def test_single_space_legacy_data_is_normalized_without_changing_geometry() -> None:
    path = Path(__file__).parents[1] / "app/data/vehicles/avante-demo-v1.json"
    vehicle = VehicleDetail.model_validate(json.loads(path.read_text(encoding="utf-8")))
    profile = vehicle.packing_profiles[0]
    assert profile.profile_id == "TRUNK_ONLY"
    assert profile.packing_space.model_dump(mode="json") == {
        "width_cm": 97,
        "depth_cm": 82,
        "height_cm": 42,
    }
    assert profile.spaces[0].opening == vehicle.opening
    assert profile.spaces[0].space_id == "TRUNK"


def test_adjacent_spaces_can_share_a_boundary(space_vehicle: dict[str, Any]) -> None:
    vehicle = VehicleDetail.model_validate(space_vehicle)
    assert vehicle.packing_profiles[0].packing_space is None
    assert len(vehicle.packing_profiles[0].spaces) == 2


@pytest.mark.parametrize("offset", [0, 0.1, 79.9])
def test_overlapping_spaces_are_rejected(space_vehicle: dict[str, Any], offset: float) -> None:
    space_vehicle["packing_profiles"][0]["spaces"][1]["origin_cm"]["y_cm"] = offset
    with pytest.raises(ValidationError, match="겹칠"):
        VehicleDetail.model_validate(space_vehicle)


@pytest.mark.parametrize("axis", ["x_cm", "y_cm", "z_cm"])
def test_negative_global_origin_is_allowed(space_vehicle: dict[str, Any], axis: str) -> None:
    space_vehicle["packing_profiles"][0]["spaces"][0]["origin_cm"][axis] = -100.1
    vehicle = VehicleDetail.model_validate(space_vehicle)
    assert vehicle.packing_profiles[0].spaces[0].origin_cm.model_dump(mode="json")[axis] == -100.1


@pytest.mark.parametrize("value", [True, "1.2", 0.01, float("nan"), float("inf")])
def test_invalid_global_origin_is_rejected(space_vehicle: dict[str, Any], value: object) -> None:
    space_vehicle["packing_profiles"][0]["spaces"][0]["origin_cm"]["x_cm"] = value
    with pytest.raises(ValidationError):
        VehicleDetail.model_validate(space_vehicle)


@pytest.mark.parametrize("dimensions", [None, {"width_cm": 99, "depth_cm": 80, "height_cm": 40}])
def test_single_space_compatibility_dimensions_must_match(
    space_vehicle: dict[str, Any], dimensions: object
) -> None:
    profile = space_vehicle["packing_profiles"][0]
    profile["spaces"].pop()
    profile["packing_space"] = dimensions
    with pytest.raises(ValidationError, match="단일 공간"):
        VehicleDetail.model_validate(space_vehicle)


def test_single_space_compatibility_dimensions_are_derived(space_vehicle: dict[str, Any]) -> None:
    profile = space_vehicle["packing_profiles"][0]
    profile["spaces"].pop()
    del profile["packing_space"]
    vehicle = VehicleDetail.model_validate(space_vehicle)
    assert vehicle.packing_profiles[0].packing_space.width_cm == 100


def test_multiple_spaces_cannot_be_replaced_by_combined_volume(
    space_vehicle: dict[str, Any],
) -> None:
    space_vehicle["packing_profiles"][0]["packing_space"] = {
        "width_cm": 100,
        "depth_cm": 120,
        "height_cm": 40,
    }
    with pytest.raises(ValidationError, match="여러 공간"):
        VehicleDetail.model_validate(space_vehicle)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("space_id", "TRUNK"),
        ("seat_codes", ["ROW_1_LEFT"]),
        ("seat_codes", ["ROW_2_LEFT"]),
        ("seat_codes", ["ROW_1_RIGHT", "ROW_1_RIGHT"]),
        ("seat_codes", []),
        ("kind", "TRUNK"),
        ("kind", "FOLDED_SEAT"),
    ],
)
def test_invalid_space_mapping_is_rejected(
    space_vehicle: dict[str, Any], field: str, value: object
) -> None:
    space_vehicle["packing_profiles"][0]["spaces"][1][field] = value
    with pytest.raises(ValidationError):
        VehicleDetail.model_validate(space_vehicle)


def test_empty_spaces_are_rejected(space_vehicle: dict[str, Any]) -> None:
    space_vehicle["packing_profiles"][0]["spaces"] = []
    with pytest.raises(ValidationError):
        VehicleDetail.model_validate(space_vehicle)


def test_same_seat_cannot_supply_two_nonoverlapping_spaces(space_vehicle: dict[str, Any]) -> None:
    spaces = space_vehicle["packing_profiles"][0]["spaces"]
    duplicate = deepcopy(spaces[1])
    duplicate["space_id"] = "SECOND_SEAT_SPACE"
    duplicate["origin_cm"]["y_cm"] = 200
    spaces.append(duplicate)
    with pytest.raises(ValidationError, match="같은 좌석"):
        VehicleDetail.model_validate(space_vehicle)


def test_folded_profile_cannot_use_regular_seat_space(space_vehicle: dict[str, Any]) -> None:
    profile = deepcopy(space_vehicle["packing_profiles"][0])
    profile["profile_id"] = "FOLDED"
    profile["target_folded_segments"] = ["FRONT"]
    space_vehicle["packing_profiles"].append(profile)
    with pytest.raises(ValidationError, match="일반 좌석"):
        VehicleDetail.model_validate(space_vehicle)


def test_space_identity_cannot_change_between_profiles(space_vehicle: dict[str, Any]) -> None:
    profile = deepcopy(space_vehicle["packing_profiles"][0])
    profile.update(profile_id="FOLDED", target_folded_segments=["FRONT"])
    profile["spaces"][0]["width_cm"] = 90
    profile["spaces"][1].update(space_id="FRONT_FOLDED", kind="FOLDED_SEAT")
    space_vehicle["packing_profiles"].append(profile)
    with pytest.raises(ValidationError, match="동일한 공간"):
        VehicleDetail.model_validate(space_vehicle)


def test_folded_space_requires_its_entire_seat_group() -> None:
    vehicle = get_vehicle_catalog()["avante_demo"].model_dump(mode="json")
    for profile in vehicle["packing_profiles"]:
        folded = set(profile["target_folded_segments"])
        used = {space["space_id"] for space in profile["spaces"]}
        if "DEMO_REAR_LEFT" in folded:
            assert "REAR_LEFT_FOLDED" in used
            assert not {"REAR_LEFT_SEAT", "REAR_CENTER_SEAT"} & used
        else:
            assert "REAR_LEFT_FOLDED" not in used
            assert {"REAR_LEFT_SEAT", "REAR_CENTER_SEAT"} <= used
