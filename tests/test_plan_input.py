import json
from copy import deepcopy
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.plan import PlanCreate, PlanData, PlanItemCreate


@pytest.fixture
def plan_payload() -> dict[str, Any]:
    return {
        "vehicle_id": "avante_demo",
        "passenger_count": 2,
        "items": [
            {
                "name": "예시 상자",
                "width_cm": 30,
                "depth_cm": 40,
                "height_cm": 41.5,
                "quantity": 2,
            }
        ],
    }


def test_plan_input_preserves_precision_order_and_separate_items(
    plan_payload: dict[str, Any],
) -> None:
    plan_payload["vehicle_id"] = " avante_demo "
    plan_payload["items"][0]["name"] = "  예시 상자  "
    plan_payload["items"].append(deepcopy(plan_payload["items"][0]))
    plan_payload["items"].append({**plan_payload["items"][0], "name": "마지막 상자"})
    plan = PlanCreate.model_validate(plan_payload)
    assert plan.vehicle_id == "avante_demo"
    assert [item.name for item in plan.items] == ["예시 상자", "예시 상자", "마지막 상자"]
    assert plan.items[0].height_cm == Decimal("41.5")
    assert plan.car_seats == plan.retained_seats == []


def test_each_seat_list_preserves_request_order_and_overlap(
    plan_payload: dict[str, Any],
) -> None:
    plan_payload["passenger_count"] = 1
    plan_payload["car_seats"] = ["ROW_2_RIGHT", "ROW_2_LEFT"]
    plan_payload["retained_seats"] = ["ROW_2_RIGHT", "ROW_1_LEFT"]
    plan = PlanCreate.model_validate(plan_payload)
    assert plan.car_seats == ["ROW_2_RIGHT", "ROW_2_LEFT"]
    assert plan.retained_seats == ["ROW_2_RIGHT", "ROW_1_LEFT"]


@pytest.mark.parametrize(
    "dimension",
    [0, -0.1, True, False, "41.5", 41.55, None, float("nan"), float("inf"), 10**309],
)
def test_invalid_dimension_is_rejected(plan_payload: dict[str, Any], dimension: object) -> None:
    plan_payload["items"][0]["height_cm"] = dimension
    with pytest.raises(ValidationError) as error:
        PlanCreate.model_validate(plan_payload)
    assert error.value.errors()[0]["loc"] == ("items", 0, "height_cm")


@pytest.mark.parametrize(
    "dimension,accepted", [(9007199254740992, True), (9007199254740993, False)]
)
def test_dimension_must_preserve_precision_in_numeric_response(
    plan_payload: dict[str, Any], dimension: int, accepted: bool
) -> None:
    plan_payload["items"][0]["width_cm"] = dimension
    if accepted:
        plan = PlanCreate.model_validate(plan_payload)
        serialized = json.loads(plan.model_dump_json())["items"][0]["width_cm"]
        assert Decimal(str(serialized)) == Decimal(dimension)
    else:
        with pytest.raises(ValidationError, match="정밀도 손실"):
            PlanCreate.model_validate(plan_payload)


@pytest.mark.parametrize("field", ["width_cm", "depth_cm", "height_cm"])
def test_every_dimension_requires_positive_numeric_cm(
    plan_payload: dict[str, Any], field: str
) -> None:
    plan_payload["items"][0][field] = "30"
    with pytest.raises(ValidationError):
        PlanCreate.model_validate(plan_payload)


@pytest.mark.parametrize("quantity", [0, -1, True, 1.0, 1.5, "1", 2147483648])
def test_quantity_requires_integer_within_database_range(
    plan_payload: dict[str, Any], quantity: object
) -> None:
    plan_payload["items"][0]["quantity"] = quantity
    with pytest.raises(ValidationError):
        PlanCreate.model_validate(plan_payload)


@pytest.mark.parametrize("passenger_count", [0, -1, True, 2.0, "2", 10])
def test_passenger_count_requires_supported_integer_range(
    plan_payload: dict[str, Any], passenger_count: object
) -> None:
    plan_payload["passenger_count"] = passenger_count
    with pytest.raises(ValidationError):
        PlanCreate.model_validate(plan_payload)


@pytest.mark.parametrize("name", ["", " \t ", "가" * 101, None, 1])
def test_item_name_requires_nonempty_text_of_at_most_100_characters(
    plan_payload: dict[str, Any], name: object
) -> None:
    plan_payload["items"][0]["name"] = name
    with pytest.raises(ValidationError):
        PlanCreate.model_validate(plan_payload)


@pytest.mark.parametrize("field", ["vehicle_id", "name"])
def test_null_character_is_rejected_before_database_write(
    plan_payload: dict[str, Any], field: str
) -> None:
    target = plan_payload if field == "vehicle_id" else plan_payload["items"][0]
    target[field] = "a\x00b"
    with pytest.raises(ValidationError, match="널 문자") as error:
        PlanCreate.model_validate(plan_payload)
    expected = ("vehicle_id",) if field == "vehicle_id" else ("items", 0, "name")
    assert error.value.errors()[0]["loc"] == expected


@pytest.mark.parametrize("field", ["car_seats", "retained_seats"])
@pytest.mark.parametrize("seats", [None, ["ROW_2_LEFT", "ROW_2_LEFT"], ["UNKNOWN"]])
def test_invalid_seat_arrays_are_rejected(
    plan_payload: dict[str, Any], field: str, seats: object
) -> None:
    plan_payload[field] = seats
    with pytest.raises(ValidationError):
        PlanCreate.model_validate(plan_payload)


@pytest.mark.parametrize("field", ["vehicle_id", "passenger_count", "items"])
def test_missing_required_plan_fields_are_rejected(
    plan_payload: dict[str, Any], field: str
) -> None:
    del plan_payload[field]
    with pytest.raises(ValidationError):
        PlanCreate.model_validate(plan_payload)


@pytest.mark.parametrize("items", [[], None])
def test_items_must_be_a_nonempty_array(plan_payload: dict[str, Any], items: object) -> None:
    plan_payload["items"] = items
    with pytest.raises(ValidationError):
        PlanCreate.model_validate(plan_payload)


def test_client_cannot_supply_user_or_item_identifiers(plan_payload: dict[str, Any]) -> None:
    plan_payload["user_id"] = "another-user"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PlanCreate.model_validate(plan_payload)
    del plan_payload["user_id"]
    plan_payload["items"][0]["item_id"] = str(uuid4())
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PlanCreate.model_validate(plan_payload)


def test_dimension_larger_than_trunk_can_be_saved(plan_payload: dict[str, Any]) -> None:
    plan_payload["items"][0]["width_cm"] = 1000
    assert PlanCreate.model_validate(plan_payload).items[0].width_cm == Decimal("1000")


def test_plan_response_has_numeric_dimensions_and_utc_time(plan_payload: dict[str, Any]) -> None:
    request = PlanCreate.model_validate(plan_payload)
    payload = {
        **request.model_dump(),
        "plan_id": uuid4(),
        "items": [{**item.model_dump(), "item_id": uuid4()} for item in request.items],
        "created_at": "2026-11-11T11:00:00+09:00",
    }
    plan = PlanData.model_validate(payload)
    response = json.loads(plan.model_dump_json())
    assert response["created_at"] == "2026-11-11T02:00:00Z"
    assert response["latest_calculation_id"] is None
    assert response["items"][0]["height_cm"] == 41.5
    assert isinstance(response["items"][0]["height_cm"], float)
    payload["created_at"] = "2026-11-11T02:00:00"
    with pytest.raises(ValidationError, match="timezone_aware"):
        PlanData.model_validate(payload)


def test_request_and_response_schemas_describe_numeric_cm() -> None:
    for mode in ("validation", "serialization"):
        schema = PlanItemCreate.model_json_schema(mode=mode)
        for field in ("width_cm", "depth_cm", "height_cm"):
            assert schema["properties"][field]["type"] == "number"
    request = PlanCreate.model_json_schema()
    assert request["additionalProperties"] is False
    assert request["properties"]["items"]["minItems"] == 1
    assert request["$defs"]["PlanItemCreate"]["properties"]["height_cm"]["multipleOf"] == 0.1
