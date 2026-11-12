from copy import deepcopy
from decimal import Decimal

import pytest

from app.schemas.packing import PackingProblem
from app.services.packing import cm_to_mm, pack, validate_packing


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


def problem(items, size=(4, 2, 4), spaces=None):
    return PackingProblem.model_validate(
        {
            "items": items,
            "spaces": spaces
            or [
                {
                    "space_id": "TRUNK",
                    "width_cm": size[0],
                    "depth_cm": size[1],
                    "height_cm": size[2],
                }
            ],
        }
    )


def assessment(lower="box", upper="box", allowed=True):
    return {
        "lower_item_id": lower,
        "upper_item_id": upper,
        "allowed": allowed,
        "basis": "LLM_ESTIMATE",
        "reason": "검사 예시",
    }


@pytest.mark.parametrize("value,expected", [(41.5, 415), (Decimal("0.1"), 1), (0, 0)])
def test_integer_millimeters(value, expected):
    assert cm_to_mm(value) == expected


@pytest.mark.parametrize("value", [True, "41.5", 41.55, -1, float("inf")])
def test_invalid_millimeters(value):
    with pytest.raises(ValueError):
        cm_to_mm(value)


def test_multiple_units_in_one_space_and_repeated_identity():
    spec = problem([item(quantity=2)])
    result = pack(spec)
    assert result.result_code == "LAYOUT_FOUND"
    assert {row["unit_index"] for row in result.placements} == {1, 2}
    assert all(row["z_cm"] == 0 for row in result.placements)
    validate_packing(spec, result.placements)


@pytest.mark.parametrize(
    "orientation,size",
    [
        ("WDH", (2, 3, 4)),
        ("WHD", (2, 4, 3)),
        ("DWH", (3, 2, 4)),
        ("DHW", (3, 4, 2)),
        ("HWD", (4, 2, 3)),
        ("HDW", (4, 3, 2)),
    ],
)
def test_all_six_rotations(orientation, size):
    result = pack(problem([item(size=(2, 3, 4), allowed_orientations=[orientation])], size=size))
    assert result.result_code == "LAYOUT_FOUND"
    assert result.placements[0]["orientation"] == orientation


@pytest.mark.parametrize("allowed", [False, None])
def test_unapproved_stacking_is_not_used(allowed):
    spec = problem([item(quantity=2)], size=(2, 2, 4))
    assert (
        pack(spec, stacking_assessments=[assessment(allowed=allowed)]).result_code
        == "NO_LAYOUT_FOUND"
    )


def test_stacking_uses_pair_and_preserves_support_order():
    spec = problem([item(quantity=2)], size=(2, 2, 4))
    result = pack(spec, stacking_assessments=[assessment()])
    assert result.result_code == "LAYOUT_FOUND"
    bottom, top = result.placements
    assert bottom["z_cm"] == 0 and top["z_cm"] == 2
    assert top["supporting_placement_ids"] == [bottom["placement_id"]]
    validate_packing(spec, result.placements, stacking_assessments=[assessment()])
    changed = deepcopy(result.placements)
    changed[1]["z_cm"] = 2.1
    with pytest.raises(ValueError):
        validate_packing(spec, changed, stacking_assessments=[assessment()])


def test_full_bottom_support_can_be_union_of_two_boxes():
    spec = problem([item("lower", 2), item("upper", size=(4, 2, 1))], size=(4, 2, 3))
    result = pack(spec, stacking_assessments=[assessment("lower", "upper")])
    assert result.result_code == "LAYOUT_FOUND"
    top = next(row for row in result.placements if row["item_id"] == "upper")
    assert len(top["supporting_placement_ids"]) == 2
    validate_packing(spec, result.placements, stacking_assessments=[assessment("lower", "upper")])


def test_partial_support_is_rejected():
    spec = problem([item("lower"), item("upper", size=(3, 2, 1))], size=(3, 2, 3))
    result = pack(spec, stacking_assessments=[assessment("lower", "upper")])
    assert result.result_code == "NO_LAYOUT_FOUND"


def test_separate_spaces_do_not_make_one_long_space():
    spaces = [
        {"space_id": str(index), "width_cm": 2, "depth_cm": 2, "height_cm": 2} for index in range(2)
    ]
    assert pack(problem([item(quantity=2)], spaces=spaces)).result_code == "LAYOUT_FOUND"
    assert pack(problem([item(size=(4, 2, 2))], spaces=spaces)).result_code == "NO_LAYOUT_FOUND"


def test_opening_is_checked_after_rotation():
    spaces = [
        {
            "space_id": "TRUNK",
            "width_cm": 4,
            "depth_cm": 4,
            "height_cm": 4,
            "opening": {"width_cm": 2, "height_cm": 2},
        }
    ]
    assert pack(problem([item(size=(3, 2, 2))], spaces=spaces)).result_code == "NO_LAYOUT_FOUND"
    assert (
        pack(
            problem([item(size=(3, 2, 2), allowed_orientations=["DWH"])], spaces=spaces)
        ).result_code
        == "LAYOUT_FOUND"
    )


def test_geometric_validator_rejects_overlap_and_wrong_unit():
    spec = problem([item(quantity=2)])
    result = pack(spec)
    for field, value in (("unit_index", 1), ("x_cm", 0)):
        rows = deepcopy(result.placements)
        rows[1][field] = value
        with pytest.raises(ValueError):
            validate_packing(spec, rows)


def test_zero_timeout_returns_no_partial_layout():
    result = pack(problem([item()]), timeout_seconds=0)
    assert result.result_code == "TIME_LIMIT"
    assert not result.search_complete and result.placements == []
