import json
from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.vehicle import VehicleDetail
from app.services.vehicles import get_compatible_profiles, get_vehicle_catalog


@pytest.fixture
def folding_vehicle_data() -> dict[str, Any]:
    # 폴딩 검증 전용 합성 차량이며 실제 아반떼 규격이 아닙니다.
    combinations = [[], ["TEST_LEFT"], ["TEST_RIGHT"], ["TEST_LEFT", "TEST_RIGHT"]]
    return {
        "vehicle_id": "test_folding_vehicle",
        "name": "폴딩 검사 전용 차량",
        "data_version": "test-v1",
        "data_source": "DUMMY",
        "seat_capacity": 5,
        "seat_codes": [
            "ROW_1_LEFT",
            "ROW_1_RIGHT",
            "ROW_2_LEFT",
            "ROW_2_CENTER",
            "ROW_2_RIGHT",
        ],
        "driver_seat": "ROW_1_LEFT",
        "fold_segments": [
            {"code": "TEST_LEFT", "seat_codes": ["ROW_2_LEFT", "ROW_2_CENTER"]},
            {"code": "TEST_RIGHT", "seat_codes": ["ROW_2_RIGHT"]},
        ],
        "packing_profiles": [
            {
                "target_folded_segments": segments,
                "packing_space": {"width_cm": 100, "depth_cm": 100, "height_cm": 50},
            }
            for segments in combinations
        ],
        "opening": {"width_cm": 100, "height_cm": 41.5},
    }


def test_avante_data_has_versioned_demo_spaces_and_all_fold_combinations() -> None:
    catalog = get_vehicle_catalog()
    assert set(catalog) == {"avante_demo"}
    vehicle = catalog["avante_demo"]
    assert vehicle.data_version == "avante-demo-v2"
    assert vehicle.data_source == "DUMMY"
    assert vehicle.seat_capacity == 5
    assert vehicle.driver_seat == "ROW_1_LEFT"
    assert {segment.code: segment.seat_codes for segment in vehicle.fold_segments} == {
        "DEMO_REAR_LEFT": ("ROW_2_LEFT", "ROW_2_CENTER"),
        "DEMO_REAR_RIGHT": ("ROW_2_RIGHT",),
        "DEMO_FRONT_RIGHT": ("ROW_1_RIGHT",),
    }
    assert len(vehicle.packing_profiles) == 8
    assert len({profile.target_folded_segments for profile in vehicle.packing_profiles}) == 8
    for profile in vehicle.packing_profiles:
        assert profile.packing_space is None
        trunk = next(space for space in profile.spaces if space.space_id == "TRUNK")
        assert (trunk.width_cm, trunk.depth_cm, trunk.height_cm) == (97, 82, 42)
        assert all(space.data_source == "DUMMY" for space in profile.spaces)
        assert {seat for space in profile.spaces for seat in space.seat_codes} == {
            "ROW_1_RIGHT",
            "ROW_2_LEFT",
            "ROW_2_CENTER",
            "ROW_2_RIGHT",
        }


def test_decimal_dimensions_are_json_numbers() -> None:
    payload = json.loads(get_vehicle_catalog()["avante_demo"].model_dump_json())
    assert type(payload["opening"]["height_cm"]) is float
    assert payload["opening"]["height_cm"] == 41.5
    for key in ("width_cm", "depth_cm", "height_cm"):
        value = payload["packing_profiles"][0]["spaces"][0][key]
        assert type(value) in (int, float)


def test_vehicle_and_nested_values_are_immutable() -> None:
    vehicle = get_vehicle_catalog()["avante_demo"]
    with pytest.raises(ValidationError, match="frozen_instance"):
        vehicle.name = "변경한 이름"
    with pytest.raises(ValidationError, match="frozen_instance"):
        vehicle.packing_profiles[0].spaces[0].width_cm = 1
    with pytest.raises(ValidationError, match="frozen_instance"):
        vehicle.seat_codes += ("ROW_3_LEFT",)
    assert isinstance(vehicle.seat_codes, tuple)
    assert isinstance(vehicle.fold_segments, tuple)
    assert isinstance(vehicle.packing_profiles, tuple)


def test_catalog_changes_do_not_affect_other_calls() -> None:
    first = get_vehicle_catalog()
    second = get_vehicle_catalog()
    first.clear()
    assert set(second) == {"avante_demo"}
    assert set(get_vehicle_catalog()) == {"avante_demo"}


@pytest.mark.parametrize("location", ["packing_space", "opening"])
@pytest.mark.parametrize(
    "value", [True, False, "41.5", 0, -1, float("nan"), float("inf"), -float("inf"), 41.55]
)
def test_invalid_dimensions_rejected(
    folding_vehicle_data: dict[str, Any], location: str, value: object
) -> None:
    target = (
        folding_vehicle_data["packing_profiles"][0]["packing_space"]
        if location == "packing_space"
        else folding_vehicle_data["opening"]
    )
    target["width_cm"] = value
    with pytest.raises(ValidationError):
        VehicleDetail.model_validate(folding_vehicle_data)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("seat_capacity",), 4),
        (("seat_capacity",), True),
        (("seat_capacity",), "5"),
        (("seat_codes", 4), "ROW_2_LEFT"),
        (("driver_seat",), "ROW_3_LEFT"),
        (("fold_segments", 1, "code"), "TEST_LEFT"),
        (("fold_segments", 0, "seat_codes"), ["ROW_2_LEFT", "ROW_2_LEFT"]),
        (("fold_segments", 0, "seat_codes"), ["ROW_3_LEFT"]),
        (("fold_segments", 0, "seat_codes"), ["ROW_1_LEFT"]),
        (("fold_segments", 1, "seat_codes"), ["ROW_2_CENTER"]),
        (("packing_profiles", 1, "target_folded_segments"), ["UNKNOWN"]),
        (("packing_profiles", 1, "target_folded_segments"), ["TEST_LEFT", "TEST_LEFT"]),
        (("packing_profiles", 0, "target_folded_segments"), ["TEST_LEFT"]),
        (("packing_profiles",), []),
    ],
)
def test_invalid_vehicle_mappings_rejected(
    folding_vehicle_data: dict[str, Any], path: tuple[str | int, ...], value: object
) -> None:
    target = folding_vehicle_data
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        VehicleDetail.model_validate(folding_vehicle_data)


def test_duplicate_combination_rejected_even_when_order_differs(
    folding_vehicle_data: dict[str, Any],
) -> None:
    duplicate = deepcopy(folding_vehicle_data["packing_profiles"][-1])
    duplicate["target_folded_segments"].reverse()
    folding_vehicle_data["packing_profiles"].append(duplicate)
    with pytest.raises(ValidationError):
        VehicleDetail.model_validate(folding_vehicle_data)


def test_base_profile_is_required(folding_vehicle_data: dict[str, Any]) -> None:
    folding_vehicle_data["packing_profiles"].pop(0)
    with pytest.raises(ValidationError):
        VehicleDetail.model_validate(folding_vehicle_data)


def test_unmeasured_opening_can_be_null(folding_vehicle_data: dict[str, Any]) -> None:
    folding_vehicle_data["opening"] = None
    assert VehicleDetail.model_validate(folding_vehicle_data).opening is None


@pytest.mark.parametrize(
    ("passenger_count", "expected"),
    [
        (1, [(), ("TEST_LEFT",), ("TEST_RIGHT",), ("TEST_LEFT", "TEST_RIGHT")]),
        (2, [(), ("TEST_LEFT",), ("TEST_RIGHT",), ("TEST_LEFT", "TEST_RIGHT")]),
        (3, [(), ("TEST_LEFT",), ("TEST_RIGHT",)]),
        (4, [(), ("TEST_RIGHT",)]),
        (5, [()]),
    ],
)
def test_profiles_leave_enough_passenger_seats(
    folding_vehicle_data: dict[str, Any], passenger_count: int, expected: list[tuple[str, ...]]
) -> None:
    vehicle = VehicleDetail.model_validate(folding_vehicle_data)
    profiles = get_compatible_profiles(vehicle, passenger_count)
    assert [profile.target_folded_segments for profile in profiles] == expected


@pytest.mark.parametrize(
    ("car_seats", "expected"),
    [
        (["ROW_2_LEFT"], [(), ("TEST_RIGHT",)]),
        (["ROW_2_CENTER"], [(), ("TEST_RIGHT",)]),
        (["ROW_2_RIGHT"], [(), ("TEST_LEFT",)]),
        (["ROW_2_LEFT", "ROW_2_RIGHT"], [()]),
        (["ROW_1_RIGHT"], [(), ("TEST_LEFT",), ("TEST_RIGHT",), ("TEST_LEFT", "TEST_RIGHT")]),
    ],
)
def test_car_seats_protect_whole_segments_without_assuming_occupancy(
    folding_vehicle_data: dict[str, Any], car_seats: list[str], expected: list[tuple[str, ...]]
) -> None:
    vehicle = VehicleDetail.model_validate(folding_vehicle_data)
    profiles = get_compatible_profiles(vehicle, passenger_count=1, car_seats=car_seats)
    assert [profile.target_folded_segments for profile in profiles] == expected


@pytest.mark.parametrize("passenger_count", [0, -1, 6, True, "2", 2.0, None])
def test_invalid_passenger_count_rejected(
    folding_vehicle_data: dict[str, Any], passenger_count: object
) -> None:
    vehicle = VehicleDetail.model_validate(folding_vehicle_data)
    with pytest.raises(ValueError):
        get_compatible_profiles(vehicle, passenger_count)


@pytest.mark.parametrize(
    "car_seats", [["ROW_1_LEFT"], ["ROW_3_LEFT"], ["UNKNOWN"], ["ROW_2_LEFT", "ROW_2_LEFT"]]
)
def test_invalid_car_seats_rejected(
    folding_vehicle_data: dict[str, Any], car_seats: list[str]
) -> None:
    vehicle = VehicleDetail.model_validate(folding_vehicle_data)
    with pytest.raises(ValueError):
        get_compatible_profiles(vehicle, passenger_count=3, car_seats=car_seats)


@pytest.mark.parametrize("passenger_count", [1, 2, 3, 4, 5])
def test_demo_folding_preserves_required_passenger_capacity(passenger_count: int) -> None:
    vehicle = get_vehicle_catalog()["avante_demo"]
    profiles = get_compatible_profiles(vehicle, passenger_count)
    assert profiles[0].target_folded_segments == ()
    segments = {segment.code: set(segment.seat_codes) for segment in vehicle.fold_segments}
    for profile in profiles:
        folded_seats = set().union(*(segments[code] for code in profile.target_folded_segments))
        assert len(vehicle.seat_codes) - len(folded_seats) >= passenger_count
    if passenger_count == 5:
        assert len(profiles) == 1
    if passenger_count == 1:
        assert len(profiles) == 8
