from copy import deepcopy

import pytest

from app.core.exceptions import AppError
from app.services import recommendations
from app.services.recommendations import recommend, validate_candidate


def item(item_id="box", quantity=1, size=(2, 2, 2), **kwargs):
    return {
        "item_id": item_id,
        "name": item_id,
        "quantity": quantity,
        "width_cm": size[0],
        "depth_cm": size[1],
        "height_cm": size[2],
        "allowed_orientations": ["WDH"],
        **kwargs,
    }


def plan(items=None, **kwargs):
    return {
        "vehicle_id": "demo",
        "passenger_count": 1,
        "car_seats": [],
        "retained_seats": [],
        "items": items or [item()],
        **kwargs,
    }


def space(space_id, size, seats=(), kind="TRUNK", origin_y=0):
    return {
        "space_id": space_id,
        "kind": kind,
        "seat_codes": list(seats),
        "width_cm": size[0],
        "depth_cm": size[1],
        "height_cm": size[2],
        "origin_cm": {"x_cm": 0, "y_cm": origin_y, "z_cm": 0},
        "opening": None,
    }


def vehicle():
    trunk = space("TRUNK", (2, 2, 2))
    seat = space("FRONT", (2, 2, 2), ["ROW_1_RIGHT"], "SEAT", 3)
    folded = space("FRONT_FOLDED", (4, 2, 2), ["ROW_1_RIGHT"], "FOLDED_SEAT", 3)
    return {
        "vehicle_id": "demo",
        "data_version": "demo-1",
        "driver_seat": "ROW_1_LEFT",
        "seat_codes": ["ROW_1_LEFT", "ROW_1_RIGHT"],
        "opening": None,
        "fold_segments": [{"code": "FRONT", "seat_codes": ["ROW_1_RIGHT"]}],
        "packing_profiles": [
            {"profile_id": "BASE", "target_folded_segments": [], "spaces": [trunk, seat]},
            {"profile_id": "FOLD", "target_folded_segments": ["FRONT"], "spaces": [trunk, folded]},
        ],
    }


def known(**kwargs):
    return {"occupied_seats": ["ROW_1_LEFT"], "current_folded_segments": [], **kwargs}


def test_full_load_reranks_no_change_before_folding():
    value = recommend(plan(), vehicle(), known(), stacking_assessments=[])
    assert value["result_code"] == "LAYOUT_FOUND"
    first = value["arrangements"][0]
    assert first["target_folded_segments"] == [] and first["all_in_trunk"]
    assert first["rank"] == 1 and first["seat_change_count"] == 0
    validate_candidate(plan(), vehicle(), first, known())


def test_multiple_spaces_and_front_folding_are_used():
    value = recommend(plan([item(quantity=3)]), vehicle(), known())
    assert value["result_code"] == "LAYOUT_FOUND"
    candidate = value["arrangements"][0]
    assert candidate["target_folded_segments"] == ["FRONT"]
    assert {row["space_id"] for row in candidate["placements"]} == {"TRUNK", "FRONT_FOLDED"}
    assert candidate["packing_space"] is None
    validate_candidate(plan([item(quantity=3)]), vehicle(), candidate, known())


def test_unknown_seats_do_not_prevent_trunk_only_result():
    value = recommend(plan(), vehicle(), {})
    assert value["is_success"] and value["occupied_seats"] is None
    assert all(row["seat_change_count"] is None for row in value["arrangements"])
    assert value["warnings"] == ["STACKING_ASSESSMENT_UNAVAILABLE"]


def test_unknown_space_returns_only_needed_seat():
    value = recommend(plan([item(quantity=2)]), vehicle(), {})
    assert value["result_code"] == "STATE_REQUIRED"
    assert {row["seat_code"] for row in value["required_inputs"]} == {"ROW_1_RIGHT"}


@pytest.mark.parametrize(
    "extra",
    [{"retained_seats": ["ROW_1_RIGHT"]}, {"car_seats": ["ROW_1_RIGHT"]}, {"passenger_count": 2}],
)
def test_protected_seat_is_never_folded_or_used(extra):
    value = recommend(plan([item(quantity=2)], **extra), vehicle(), known())
    assert value["result_code"] == "NO_LAYOUT_FOUND"
    assert not value["arrangements"]


def test_explicit_passenger_assignment_is_respected():
    value = recommend(
        plan(passenger_count=2, passenger_seats=["ROW_1_LEFT", "ROW_1_RIGHT"]), vehicle(), known()
    )
    assert value["is_success"]
    assert value["arrangements"][0]["passenger_seats"] == ["ROW_1_LEFT", "ROW_1_RIGHT"]


@pytest.mark.parametrize(
    "body",
    [
        known(current_folded_segments=["FRONT"], occupied_seats=["ROW_1_LEFT", "ROW_1_RIGHT"]),
        known(vehicle_state={"seats": [{"seat_code": "ROW_1_RIGHT", "occupancy": "OCCUPIED"}]}),
    ],
)
def test_conflicting_observed_state_is_rejected(body):
    with pytest.raises(AppError) as error:
        recommend(plan(), vehicle(), body)
    assert error.value.code == "VEHICLE_STATE_CONFLICT"


def test_exclusions_compare_distinct_item_combinations_and_keep_required():
    car = vehicle()
    car["packing_profiles"] = [car["packing_profiles"][0]]
    original = plan([item("chair", 2), item("bag", 2, required_quantity=1)])
    result = recommend(original, car, known(allow_exclusions=True))
    assert result["result_code"] == "EXCLUSION_OPTIONS_FOUND" and not result["is_success"]
    exclusions = [
        dict((row["name"], row["quantity"]) for row in candidate["exclusions"])
        for candidate in result["alternatives"]
    ]
    assert {"chair": 2} in exclusions
    assert {"chair": 1, "bag": 1} in exclusions
    assert {"bag": 2} not in exclusions
    for candidate in result["alternatives"]:
        assert candidate["excluded_total_quantity"] == 2
        validate_candidate(original, car, candidate, known())


def test_no_empty_success_when_every_item_is_too_large():
    result = recommend(plan([item(size=(9, 9, 9))]), vehicle(), known(allow_exclusions=True))
    assert result["result_code"] == "NO_LAYOUT_FOUND"


def test_keep_existing_allows_unaffected_empty_seat_folding():
    original = plan()
    base = recommend(original, vehicle(), known())["arrangements"][0]
    base["input_snapshot"] = original
    updated = plan([item(), item("long", size=(4, 2, 2))])
    value = recommend(
        updated,
        vehicle(),
        known(packing_mode="KEEP_EXISTING", cargo_state="SELECTED_ARRANGEMENT"),
        base_arrangement=base,
    )
    assert value["is_success"]
    candidate = value["arrangements"][0]
    assert candidate["target_folded_segments"] == ["FRONT"]
    assert candidate["placements"][0] == base["placements"][0]


def test_keep_existing_rejects_size_changes_and_space_changes():
    original = plan()
    base = recommend(original, vehicle(), known())["arrangements"][0]
    base["input_snapshot"] = original
    request = known(packing_mode="KEEP_EXISTING", cargo_state="SELECTED_ARRANGEMENT")
    with pytest.raises(AppError) as error:
        recommend(plan([item(size=(1, 2, 2))]), vehicle(), request, base_arrangement=base)
    assert error.value.code == "BASE_LAYOUT_STALE"
    altered = deepcopy(base)
    altered["packing_spaces"][0]["origin_cm"]["x_cm"] = 1
    with pytest.raises(AppError) as error:
        recommend(original, vehicle(), request, base_arrangement=altered)
    assert error.value.code == "BASE_LAYOUT_STALE"


def test_timeout_discards_already_found_candidate(monkeypatch):
    original = recommendations.find_layout

    def deadline_after_first(*args, **kwargs):
        result = original(*args, **kwargs)
        if result:
            kwargs["budget"].deadline = 0
        return result

    monkeypatch.setattr(recommendations, "find_layout", deadline_after_first)
    result = recommend(plan(), vehicle(), known())
    assert result["result_code"] == "TIME_LIMIT"
    assert result["arrangements"] == result["alternatives"] == []
    assert not result["search_complete"]


def test_large_quantity_is_rejected_before_unit_expansion():
    with pytest.raises(AppError) as error:
        recommend(plan([item(quantity=2_147_483_647)]), vehicle(), known())
    assert error.value.code == "VALIDATION_ERROR"


def test_irrelevant_unknown_seats_do_not_trigger_questions():
    result = recommend(plan([item(size=(9, 9, 9))]), vehicle(), {})
    assert result["result_code"] == "NO_LAYOUT_FOUND"
    assert result["required_inputs"] == []


def test_empty_child_seat_does_not_imply_passenger_assignment():
    result = recommend(
        plan(car_seats=["ROW_1_RIGHT"], passenger_seats=["ROW_1_LEFT"]), vehicle(), known()
    )
    assert result["is_success"]
    assert result["arrangements"][0]["passenger_seats"] == ["ROW_1_LEFT"]


def test_observed_fold_conflict_requests_confirmation_for_fixed_space():
    original = plan([item("long", size=(4, 2, 2))])
    base = recommend(original, vehicle(), known())["arrangements"][0]
    base["input_snapshot"] = original
    request = known(packing_mode="KEEP_EXISTING", cargo_state="SELECTED_ARRANGEMENT")
    result = recommend(original, vehicle(), request, base_arrangement=base)
    assert result["result_code"] == "STATE_REQUIRED"
    assert result["required_inputs"][0]["segment_code"] == "FRONT"


def test_selection_geometry_rejects_tampered_exclusions():
    original = plan([item(quantity=4, required_quantity=3)])
    result = recommend(original, vehicle(), known(allow_exclusions=True))
    candidate = result["alternatives"][0]
    changed = deepcopy(candidate)
    changed["exclusions"][0]["quantity"] += 1
    with pytest.raises(ValueError):
        validate_candidate(original, vehicle(), changed)


def test_existing_fold_stays_known_when_another_empty_seat_folds():
    car = vehicle()
    car["seat_codes"].append("ROW_2_RIGHT")
    car["fold_segments"].append({"code": "REAR", "seat_codes": ["ROW_2_RIGHT"]})
    rear = space("REAR", (2, 2, 2), ["ROW_2_RIGHT"], "SEAT", 10)
    rear_folded = space("REAR_FOLDED", (4, 2, 2), ["ROW_2_RIGHT"], "FOLDED_SEAT", 10)
    for profile in car["packing_profiles"]:
        profile["spaces"].append(rear)
    car["packing_profiles"] += [
        {
            "profile_id": "REAR",
            "target_folded_segments": ["REAR"],
            "spaces": [*car["packing_profiles"][0]["spaces"][:2], rear_folded],
        },
        {
            "profile_id": "BOTH",
            "target_folded_segments": ["FRONT", "REAR"],
            "spaces": [*car["packing_profiles"][1]["spaces"][:2], rear_folded],
        },
    ]
    original = plan([item("long", size=(4, 2, 2))])
    choices = recommend(original, car, known())["arrangements"]
    base = next(row for row in choices if row["target_folded_segments"] == ["REAR"])
    base["input_snapshot"] = original
    changed = plan([item("long", size=(4, 2, 2)), item("new", size=(4, 2, 2))])
    result = recommend(
        changed,
        car,
        {
            "packing_mode": "KEEP_EXISTING",
            "cargo_state": "SELECTED_ARRANGEMENT",
            "vehicle_state": {"seats": [{"seat_code": "ROW_1_RIGHT", "occupancy": "EMPTY"}]},
        },
        base_arrangement=base,
    )
    assert result["is_success"]
    assert result["arrangements"][0]["target_folded_segments"] == ["FRONT", "REAR"]
    assert result["arrangements"][0]["placements"][0] == base["placements"][0]


def test_many_distinct_items_find_single_exclusion_without_enumerating_every_subset():
    car = vehicle()
    car["packing_profiles"] = [
        {"profile_id": "BASE", "target_folded_segments": [], "spaces": [space("TRUNK", (23, 1, 1))]}
    ]
    original = plan([item(str(index), size=(1, 1, 1)) for index in range(24)])
    result = recommend(original, car, known(allow_exclusions=True))
    assert result["result_code"] == "EXCLUSION_OPTIONS_FOUND"
    assert len(result["alternatives"]) == 20
    assert all(row["excluded_total_quantity"] == 1 for row in result["alternatives"])


def test_explicit_unknown_cargo_state_needs_confirmation():
    result = recommend(plan(), vehicle(), known(cargo_state="UNKNOWN"))
    assert result["result_code"] == "STATE_REQUIRED"
    assert result["required_inputs"][0]["field"] == "cargo_state"
