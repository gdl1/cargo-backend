from collections.abc import Iterator
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock
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
from app.models import Calculation, Plan, PlanCarSeat, PlanItem, PlanRetainedSeat, Vehicle

PLAN_INPUT = {
    "vehicle_id": "avante_demo",
    "passenger_count": 2,
    "car_seats": [],
    "retained_seats": [],
    "items": [
        {"name": "예시 상자", "width_cm": 30, "depth_cm": 41.5, "height_cm": 20, "quantity": 2}
    ],
}


def use_plan_session(application: FastAPI, session: Session) -> None:
    def override_db() -> Iterator[Session]:
        yield session

    application.dependency_overrides[get_db] = override_db


def plan_counts(session: Session) -> tuple[int, ...]:
    counts = tuple(
        session.scalar(select(func.count()).select_from(model))
        for model in (Plan, PlanItem, PlanCarSeat, PlanRetainedSeat)
    )
    session.commit()
    return counts


@pytest.fixture
def plan_client(application: FastAPI, model_session: Session) -> Iterator[TestClient]:
    seed_vehicles(model_session)
    model_session.commit()
    use_plan_session(application, model_session)
    with TestClient(application) as client:
        yield client


@pytest.mark.integration
class TestPlanCreation:
    def test_create_preserves_items_seats_and_server_user(
        self, plan_client: TestClient, model_session: Session, settings: Settings
    ) -> None:
        payload = deepcopy(PLAN_INPUT)
        payload["passenger_count"] = 1
        payload["car_seats"] = ["ROW_2_RIGHT"]
        payload["retained_seats"] = ["ROW_2_RIGHT", "ROW_1_LEFT"]
        payload["items"][0]["name"] = "  예시 상자  "
        payload["items"].append(deepcopy(payload["items"][0]))
        payload["items"].append(
            {"name": "긴 상자", "width_cm": 120, "depth_cm": 200, "height_cm": 90, "quantity": 1}
        )
        key = uuid4()
        response = plan_client.post(
            "/api/v1/plans",
            headers={"Idempotency-Key": str(key), "X-User-ID": "other-user"},
            params={"user_id": "query-user"},
            json=payload,
        )
        assert response.status_code == 201
        body = response.json()
        assert body["code"] == "SUCCESS"
        assert body["message"] == "적재 계획을 저장했습니다."
        data = body["data"]
        plan_id = UUID(data["plan_id"])
        assert response.headers["location"] == f"/api/v1/plans/{plan_id}"
        assert set(data) == {
            "plan_id",
            "vehicle_id",
            "passenger_count",
            "car_seats",
            "retained_seats",
            "items",
            "created_at",
            "latest_calculation_id",
            "passenger_seats",
            "revision",
            "updated_at",
            "selected_calculation_id",
            "selected_arrangement_id",
            "name",
            "category",
            "memo",
            "is_favorite",
            "favorited_at",
        }
        assert data["vehicle_id"] == "avante_demo"
        assert data["passenger_count"] == 1
        assert data["car_seats"] == ["ROW_2_RIGHT"]
        assert data["retained_seats"] == ["ROW_1_LEFT", "ROW_2_RIGHT"]
        assert data["latest_calculation_id"] is None
        assert datetime.fromisoformat(data["created_at"]).utcoffset() == timedelta(0)
        item_ids = [UUID(item["item_id"]) for item in data["items"]]
        assert len(set(item_ids)) == 3
        expected_items = [
            {
                **item,
                "name": item["name"].strip(),
                "required_quantity": 0,
                "allowed_orientations": ["WDH", "WHD", "DWH", "DHW", "HWD", "HDW"],
                "transport_state": "UNKNOWN",
                "dimension_source": "UNSPECIFIED",
                "material": None,
                "saved_item_id": None,
            }
            for item in payload["items"]
        ]
        assert [
            {key: value for key, value in item.items() if key != "item_id"}
            for item in data["items"]
        ] == expected_items
        assert isinstance(data["items"][0]["depth_cm"], float)

        plan = model_session.get(Plan, plan_id)
        assert plan.user_id == settings.demo_user_id
        assert plan.idempotency_key == key
        assert len(plan.request_hash) == 64
        assert [item.item_id for item in plan.items] == item_ids
        assert [item.position for item in plan.items] == [1, 2, 3]
        assert plan.items[0].depth_cm == Decimal("41.5")
        assert [seat.seat_code for seat in plan.car_seats] == data["car_seats"]
        assert [seat.seat_code for seat in plan.retained_seats] == data["retained_seats"]
        assert plan_counts(model_session) == (1, 3, 1, 2)

    def test_normalized_retry_returns_original_response(
        self, plan_client: TestClient, model_session: Session
    ) -> None:
        payload = deepcopy(PLAN_INPUT)
        payload["car_seats"] = ["ROW_2_RIGHT", "ROW_2_LEFT"]
        payload["retained_seats"] = ["ROW_2_CENTER", "ROW_1_LEFT"]
        payload["items"][0]["name"] = " 예시 상자 "
        headers = {"Idempotency-Key": str(uuid4())}
        first = plan_client.post("/api/v1/plans", json=payload, headers=headers)
        assert first.status_code == 201

        normalized = deepcopy(payload)
        normalized["car_seats"].reverse()
        normalized["retained_seats"].reverse()
        normalized["items"][0]["name"] = "예시 상자"
        normalized["items"][0]["width_cm"] = 30.0
        second = plan_client.post("/api/v1/plans", json=normalized, headers=headers)
        assert second.status_code == 200
        assert second.json() == first.json()
        assert second.headers["location"] == first.headers["location"]
        assert plan_counts(model_session) == (1, 1, 2, 2)

    def test_omitted_seats_and_explicit_empty_seats_are_same_request(
        self, plan_client: TestClient, model_session: Session
    ) -> None:
        payload = deepcopy(PLAN_INPUT)
        payload.pop("car_seats")
        payload.pop("retained_seats")
        headers = {"Idempotency-Key": str(uuid4())}
        first = plan_client.post("/api/v1/plans", json=payload, headers=headers)
        second = plan_client.post("/api/v1/plans", json=PLAN_INPUT, headers=headers)
        assert first.status_code == 201
        assert second.status_code == 200
        assert second.json() == first.json()
        assert first.json()["data"]["retained_seats"] == []
        assert plan_counts(model_session) == (1, 1, 0, 0)

    def test_changed_input_with_same_key_is_rejected(
        self, plan_client: TestClient, model_session: Session
    ) -> None:
        headers = {"Idempotency-Key": str(uuid4())}
        first = plan_client.post("/api/v1/plans", json=PLAN_INPUT, headers=headers)
        assert first.status_code == 201
        payload = deepcopy(PLAN_INPUT)
        payload["items"][0]["quantity"] = 3
        conflict = plan_client.post("/api/v1/plans", json=payload, headers=headers)
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
        assert plan_counts(model_session) == (1, 1, 0, 0)
        retry = plan_client.post("/api/v1/plans", json=PLAN_INPUT, headers=headers)
        assert retry.status_code == 200
        assert retry.json() == first.json()

    def test_item_order_is_part_of_request_identity(self, plan_client: TestClient) -> None:
        payload = deepcopy(PLAN_INPUT)
        payload["items"].append(
            {"name": "다른 상자", "width_cm": 10, "depth_cm": 20, "height_cm": 30, "quantity": 1}
        )
        headers = {"Idempotency-Key": str(uuid4())}
        assert plan_client.post("/api/v1/plans", json=payload, headers=headers).status_code == 201
        payload["items"].reverse()
        response = plan_client.post("/api/v1/plans", json=payload, headers=headers)
        assert response.status_code == 409
        assert response.json()["code"] == "IDEMPOTENCY_KEY_REUSED"

    def test_new_key_creates_another_plan(
        self, plan_client: TestClient, model_session: Session
    ) -> None:
        responses = [
            plan_client.post(
                "/api/v1/plans", json=PLAN_INPUT, headers={"Idempotency-Key": str(uuid4())}
            )
            for _ in range(2)
        ]
        assert [response.status_code for response in responses] == [201, 201]
        assert responses[0].json()["data"]["plan_id"] != responses[1].json()["data"]["plan_id"]
        assert plan_counts(model_session) == (2, 2, 0, 0)

    def test_server_user_scopes_same_key(
        self, plan_client: TestClient, model_session: Session, settings: Settings
    ) -> None:
        headers = {"Idempotency-Key": str(uuid4())}
        first = plan_client.post("/api/v1/plans", json=PLAN_INPUT, headers=headers)
        other_app = create_app(settings.model_copy(update={"demo_user_id": "other-demo"}))
        use_plan_session(other_app, model_session)
        with TestClient(other_app) as other_client:
            second = other_client.post("/api/v1/plans", json=PLAN_INPUT, headers=headers)
        assert first.status_code == second.status_code == 201
        assert first.json()["data"]["plan_id"] != second.json()["data"]["plan_id"]
        assert set(model_session.scalars(select(Plan.user_id))) == {
            settings.demo_user_id,
            "other-demo",
        }
        assert plan_counts(model_session) == (2, 2, 0, 0)

    @pytest.mark.parametrize("vehicle_id", ["unknown", "db_only_vehicle"])
    def test_unknown_or_unconfigured_vehicle_is_rejected_without_partial_rows(
        self, plan_client: TestClient, model_session: Session, vehicle_id: str
    ) -> None:
        model_session.add(Vehicle(vehicle_id="db_only_vehicle", name="규격이 없는 차량"))
        model_session.commit()
        payload = {**PLAN_INPUT, "vehicle_id": vehicle_id}
        response = plan_client.post(
            "/api/v1/plans", json=payload, headers={"Idempotency-Key": str(uuid4())}
        )
        assert response.status_code == 404
        assert response.json()["code"] == "VEHICLE_NOT_FOUND"
        assert plan_counts(model_session) == (0, 0, 0, 0)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("passenger_count", 6),
            ("car_seats", ["ROW_1_LEFT"]),
            ("car_seats", ["ROW_3_LEFT"]),
            ("retained_seats", ["ROW_1_CENTER"]),
            ("car_seats", ["ROW_3_LEFT", "ROW_2_RIGHT"]),
            ("retained_seats", ["ROW_3_LEFT", "ROW_2_LEFT"]),
        ],
    )
    def test_vehicle_constraints_preserve_request_error_location_and_do_not_reserve_key(
        self, plan_client: TestClient, model_session: Session, field: str, value: object
    ) -> None:
        headers = {"Idempotency-Key": str(uuid4())}
        response = plan_client.post(
            "/api/v1/plans", json={**PLAN_INPUT, field: value}, headers=headers
        )
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "VALIDATION_ERROR"
        assert any(error["loc"][:2] == ["body", field] for error in body["data"]["errors"])
        if field in ("car_seats", "retained_seats"):
            assert any(error["loc"] == ["body", field, 0] for error in body["data"]["errors"])
        assert plan_counts(model_session) == (0, 0, 0, 0)
        retry = plan_client.post("/api/v1/plans", json=PLAN_INPUT, headers=headers)
        assert retry.status_code == 201

    def test_retry_keeps_creation_response_after_later_calculation(
        self, plan_client: TestClient, model_session: Session
    ) -> None:
        headers = {"Idempotency-Key": str(uuid4())}
        first = plan_client.post("/api/v1/plans", json=PLAN_INPUT, headers=headers)
        assert first.status_code == 201
        model_session.add(
            Calculation(
                plan_id=UUID(first.json()["data"]["plan_id"]),
                is_success=False,
                current_folded_segments=None,
            )
        )
        model_session.commit()
        retry = plan_client.post("/api/v1/plans", json=PLAN_INPUT, headers=headers)
        assert retry.status_code == 200
        assert retry.json() == first.json()
        assert retry.json()["data"]["latest_calculation_id"] is None


@pytest.fixture
def plan_validation_client(application: FastAPI) -> Iterator[TestClient]:
    session = Mock(spec=Session)
    use_plan_session(application, session)
    with TestClient(application) as client:
        yield client
    session.begin.assert_not_called()


@pytest.mark.parametrize("key", [None, "not-a-uuid"])
def test_invalid_idempotency_header_is_located(
    plan_validation_client: TestClient, key: str | None
) -> None:
    headers = {} if key is None else {"Idempotency-Key": key}
    response = plan_validation_client.post("/api/v1/plans", json=PLAN_INPUT, headers=headers)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert any(error["loc"][0] == "header" for error in body["data"]["errors"])


@pytest.mark.parametrize(
    ("field", "value"),
    [("passenger_count", True), ("items", []), ("retained_seats", None), ("user_id", "spoof")],
)
def test_invalid_body_is_rejected_before_database_write(
    plan_validation_client: TestClient, field: str, value: object
) -> None:
    response = plan_validation_client.post(
        "/api/v1/plans",
        json={**PLAN_INPUT, field: value},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert any(error["loc"][:2] == ["body", field] for error in body["data"]["errors"])


def test_plan_openapi_documents_header_and_success_statuses(application: FastAPI) -> None:
    operation = application.openapi()["paths"]["/api/v1/plans"]["post"]
    assert {"200", "201", "404", "409", "422", "500"} <= set(operation["responses"])
    assert any(
        parameter["in"] == "header"
        and parameter["name"].lower() == "idempotency-key"
        and parameter["required"]
        for parameter in operation["parameters"]
    )
