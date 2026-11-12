from collections.abc import Iterator
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.seed import seed_vehicles
from app.db.session import get_db
from app.main import create_app
from app.models import Calculation, Plan, Vehicle

pytestmark = pytest.mark.integration

PLAN_INPUT = {
    "vehicle_id": "avante_demo",
    "passenger_count": 2,
    "car_seats": ["ROW_2_RIGHT", "ROW_2_LEFT"],
    "retained_seats": ["ROW_2_CENTER", "ROW_1_LEFT"],
    "items": [
        {"name": "첫 상자", "width_cm": 30, "depth_cm": 41.5, "height_cm": 20, "quantity": 2},
        {"name": "둘째 상자", "width_cm": 15, "depth_cm": 20, "height_cm": 10, "quantity": 3},
    ],
}
NOT_FOUND = {"code": "PLAN_NOT_FOUND", "message": "적재 계획을 찾을 수 없습니다.", "data": None}
TIME = datetime(2026, 11, 11, 2, tzinfo=UTC)


def use_session(application: FastAPI, session: Session) -> None:
    def override_db() -> Iterator[Session]:
        yield session

    application.dependency_overrides[get_db] = override_db


@pytest.fixture
def query_client(application: FastAPI, model_session: Session) -> Iterator[TestClient]:
    seed_vehicles(model_session)
    model_session.commit()
    use_session(application, model_session)
    with TestClient(application) as client:
        yield client


def create_plan(client: TestClient, session: Session, name: str) -> tuple[dict, dict, dict]:
    payload = deepcopy(PLAN_INPUT)
    payload["items"][0]["name"] = name
    headers = {"Idempotency-Key": str(uuid4())}
    session.commit()
    response = client.post("/api/v1/plans", json=payload, headers=headers)
    assert response.status_code == 201
    return response.json()["data"], payload, headers


@pytest.fixture
def two_plans(query_client: TestClient, model_session: Session) -> tuple[dict, dict]:
    first, _, _ = create_plan(query_client, model_session, "첫 계획 상자")
    second, _, _ = create_plan(query_client, model_session, "둘째 계획 상자")
    return first, second


def test_empty_list_and_missing_detail(query_client: TestClient) -> None:
    response = query_client.get("/api/v1/plans")
    assert response.status_code == 200
    assert response.json() == {
        "code": "SUCCESS",
        "message": "적재 계획 목록을 조회했습니다.",
        "data": {"plans": [], "limit": 20, "offset": 0, "total": 0},
    }
    missing = query_client.get(f"/api/v1/plans/{uuid4()}")
    assert missing.status_code == 404
    assert missing.json() == NOT_FOUND


def test_list_summaries_match_saved_plans(
    query_client: TestClient, two_plans: tuple[dict, dict]
) -> None:
    response = query_client.get("/api/v1/plans")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "SUCCESS"
    assert body["message"] == "적재 계획 목록을 조회했습니다."
    data = body["data"]
    assert (data["limit"], data["offset"], data["total"]) == (20, 0, 2)
    originals = {plan["plan_id"]: plan for plan in two_plans}
    assert {plan["plan_id"] for plan in data["plans"]} == set(originals)
    for summary in data["plans"]:
        original = originals[summary["plan_id"]]
        assert summary == {
            "plan_id": original["plan_id"],
            "vehicle_id": "avante_demo",
            "vehicle_name": "아반떼 데모",
            "passenger_count": 2,
            "item_type_count": 2,
            "item_total_quantity": 5,
            "created_at": original["created_at"],
            "latest_calculation_id": None,
            "revision": original["revision"],
            "updated_at": original["updated_at"],
            "selected_calculation_id": None,
            "selected_arrangement_id": None,
            "name": None,
            "category": None,
            "memo": None,
            "is_favorite": False,
            "favorited_at": None,
            "item_summaries": [
                {"item_id": item["item_id"], "name": item["name"], "quantity": item["quantity"]}
                for item in original["items"]
            ],
        }


def test_detail_preserves_each_saved_plan(
    query_client: TestClient, two_plans: tuple[dict, dict]
) -> None:
    for original in two_plans:
        response = query_client.get(f"/api/v1/plans/{original['plan_id']}")
        assert response.status_code == 200
        assert response.json() == {
            "code": "SUCCESS",
            "message": "적재 계획을 조회했습니다.",
            "data": original,
        }
        data = response.json()["data"]
        assert isinstance(data["items"][0]["depth_cm"], float)
        assert data["items"][0]["depth_cm"] == 41.5
        assert datetime.fromisoformat(data["created_at"]).utcoffset() == timedelta(0)
        assert not {"user_id", "idempotency_key", "request_hash"} & set(data)


def test_order_pagination_and_empty_page_keep_owner_total(
    query_client: TestClient, model_session: Session, settings: Settings
) -> None:
    low, high, newest = [UUID(int=value) for value in (1, 2, 3)]
    for plan_id, created_at in ((high, TIME), (newest, TIME + timedelta(days=1)), (low, TIME)):
        model_session.add(
            Plan(
                plan_id=plan_id,
                user_id=settings.demo_user_id,
                vehicle_id="avante_demo",
                passenger_count=1,
                created_at=created_at,
            )
        )
    model_session.add(
        Plan(user_id="other-user", vehicle_id="avante_demo", passenger_count=1, created_at=TIME)
    )
    model_session.commit()
    expected = [str(newest), str(high), str(low)]
    full = query_client.get("/api/v1/plans").json()["data"]
    assert [row["plan_id"] for row in full["plans"]] == expected
    assert full["total"] == 3
    for offset in (0, 1, 2, 3, 9223372036854775807):
        response = query_client.get("/api/v1/plans", params={"limit": 1, "offset": offset})
        assert response.status_code == 200
        data = response.json()["data"]
        assert (data["limit"], data["offset"], data["total"]) == (1, offset, 3)
        assert [row["plan_id"] for row in data["plans"]] == expected[offset : offset + 1]
    assert model_session.scalar(select(func.count()).select_from(Plan)) == 4


def test_owner_scope_ignores_client_user_and_hides_foreign_plan(
    query_client: TestClient,
    two_plans: tuple[dict, dict],
    model_session: Session,
    settings: Settings,
) -> None:
    other = Plan(user_id="other-user", vehicle_id="avante_demo", passenger_count=1)
    model_session.add(other)
    model_session.commit()
    other_id = str(other.plan_id)
    model_session.commit()
    response = query_client.get(
        "/api/v1/plans", params={"user_id": "other-user"}, headers={"X-User-ID": "other-user"}
    )
    assert response.status_code == 200
    assert response.json()["data"]["total"] == 2
    assert {row["plan_id"] for row in response.json()["data"]["plans"]} == {
        plan["plan_id"] for plan in two_plans
    }
    foreign = query_client.get(f"/api/v1/plans/{other_id}", headers={"X-User-ID": "other-user"})
    missing = query_client.get(f"/api/v1/plans/{uuid4()}")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == NOT_FOUND

    other_app = create_app(settings.model_copy(update={"demo_user_id": "other-user"}))
    use_session(other_app, model_session)
    with TestClient(other_app) as other_client:
        data = other_client.get("/api/v1/plans").json()["data"]
        assert data["total"] == 1
        assert [row["plan_id"] for row in data["plans"]] == [other_id]
        assert other_client.get(f"/api/v1/plans/{other_id}").status_code == 200
        assert other_client.get(f"/api/v1/plans/{two_plans[0]['plan_id']}").json() == NOT_FOUND


def test_latest_calculation_and_counts_do_not_change_plan_order(
    query_client: TestClient, two_plans: tuple[dict, dict], model_session: Session
) -> None:
    before = query_client.get("/api/v1/plans").json()["data"]
    first_id, second_id = (UUID(plan["plan_id"]) for plan in two_plans)
    older_success, tied_success, latest_failure, other_plan_calc = [
        UUID(int=value) for value in (10, 20, 30, 40)
    ]
    for calculation_id, plan_id, created_at, success in (
        (latest_failure, first_id, TIME + timedelta(days=1), False),
        (older_success, first_id, TIME, True),
        (tied_success, first_id, TIME + timedelta(days=1), True),
        (other_plan_calc, second_id, TIME + timedelta(days=2), True),
    ):
        model_session.add(
            Calculation(
                calculation_id=calculation_id,
                plan_id=plan_id,
                created_at=created_at,
                is_success=success,
                current_folded_segments=None,
            )
        )
    model_session.commit()
    after = query_client.get("/api/v1/plans").json()["data"]
    assert [row["plan_id"] for row in after["plans"]] == [row["plan_id"] for row in before["plans"]]
    expected_latest = {str(first_id): str(latest_failure), str(second_id): str(other_plan_calc)}
    previous = {row["plan_id"]: row for row in before["plans"]}
    for summary in after["plans"]:
        plan_id = summary["plan_id"]
        assert summary == {**previous[plan_id], "latest_calculation_id": expected_latest[plan_id]}
        detail = query_client.get(f"/api/v1/plans/{plan_id}").json()["data"]
        assert detail["latest_calculation_id"] == expected_latest[plan_id]
        original = next(plan for plan in two_plans if plan["plan_id"] == plan_id)
        assert detail == {**original, "latest_calculation_id": expected_latest[plan_id]}


def test_creation_retry_keeps_original_response_while_get_shows_latest(
    query_client: TestClient, model_session: Session
) -> None:
    original, payload, headers = create_plan(query_client, model_session, "재시도 상자")
    calculation_id = uuid4()
    model_session.add(
        Calculation(
            calculation_id=calculation_id,
            plan_id=UUID(original["plan_id"]),
            is_success=False,
            current_folded_segments=None,
        )
    )
    model_session.commit()
    detail = query_client.get(f"/api/v1/plans/{original['plan_id']}")
    assert detail.json()["data"]["latest_calculation_id"] == str(calculation_id)
    model_session.commit()
    retry = query_client.post("/api/v1/plans", json=payload, headers=headers)
    assert retry.status_code == 200
    assert retry.json()["data"] == original


def test_quantity_total_can_exceed_single_item_integer_range(
    query_client: TestClient, model_session: Session
) -> None:
    payload = deepcopy(PLAN_INPUT)
    for item in payload["items"]:
        item["quantity"] = 2147483647
    response = query_client.post(
        "/api/v1/plans", json=payload, headers={"Idempotency-Key": str(uuid4())}
    )
    assert response.status_code == 201
    summary = query_client.get("/api/v1/plans").json()["data"]["plans"][0]
    assert summary["item_type_count"] == 2
    assert summary["item_total_quantity"] == 4294967294


def test_saved_plan_for_vehicle_outside_current_catalog_is_readable(
    query_client: TestClient, model_session: Session, settings: Settings
) -> None:
    vehicle = Vehicle(vehicle_id="retired_vehicle", name="기존 차량")
    plan = Plan(user_id=settings.demo_user_id, vehicle=vehicle, passenger_count=1)
    model_session.add(plan)
    model_session.commit()
    plan_id = str(plan.plan_id)
    model_session.commit()
    summary = query_client.get("/api/v1/plans").json()["data"]["plans"][0]
    assert summary["plan_id"] == plan_id
    assert summary["vehicle_name"] == "기존 차량"
    assert (summary["item_type_count"], summary["item_total_quantity"]) == (0, 0)
    detail = query_client.get(f"/api/v1/plans/{plan_id}")
    assert detail.status_code == 200
    assert detail.json()["data"]["vehicle_id"] == "retired_vehicle"
    assert detail.json()["data"]["items"] == []
    assert detail.json()["data"]["car_seats"] == []
    assert detail.json()["data"]["retained_seats"] == []


def test_two_clients_can_select_and_read_both_plans(
    query_client: TestClient,
    two_plans: tuple[dict, dict],
    model_session: Session,
    settings: Settings,
) -> None:
    pleos_app = create_app(settings)
    use_session(pleos_app, model_session)
    with TestClient(pleos_app) as pleos:
        mobile_list = query_client.get("/api/v1/plans")
        pleos_list = pleos.get("/api/v1/plans")
        assert mobile_list.status_code == pleos_list.status_code == 200
        assert mobile_list.json() == pleos_list.json()
        assert pleos_list.json()["data"]["total"] == 2
        for selected in pleos_list.json()["data"]["plans"]:
            path = f"/api/v1/plans/{selected['plan_id']}"
            mobile_detail = query_client.get(path)
            pleos_detail = pleos.get(path)
            assert mobile_detail.status_code == pleos_detail.status_code == 200
            assert mobile_detail.json() == pleos_detail.json()
            assert pleos_detail.json()["data"] in two_plans
