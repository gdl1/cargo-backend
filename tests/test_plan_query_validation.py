from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.plan import PlanSummary


@pytest.fixture
def query_session(application: FastAPI) -> Mock:
    session = Mock(spec=Session)

    def override_db() -> Iterator[Session]:
        yield session

    application.dependency_overrides[get_db] = override_db
    return session


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("limit", "0"),
        ("limit", "101"),
        ("limit", "-1"),
        ("limit", "1.0"),
        ("limit", "true"),
        ("limit", ""),
        ("offset", "-1"),
        ("offset", "0.5"),
        ("offset", "1.0"),
        ("offset", "false"),
        ("offset", "9223372036854775808"),
    ],
)
def test_invalid_pagination_is_rejected_before_database(
    application: FastAPI, query_session: Mock, field: str, value: str
) -> None:
    with TestClient(application) as client:
        response = client.get("/api/v1/plans", params={field: value})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert any(error["loc"] == ["query", field] for error in body["data"]["errors"])
    assert query_session.mock_calls == []


def test_invalid_plan_id_is_rejected_before_database(
    application: FastAPI, query_session: Mock
) -> None:
    with TestClient(application) as client:
        response = client.get("/api/v1/plans/not-a-uuid")
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["data"]["errors"][0]["loc"] == ["path", "plan_id"]
    assert query_session.mock_calls == []


@pytest.mark.parametrize("path", ["/api/v1/plans", f"/api/v1/plans/{uuid4()}"])
def test_query_database_failure_hides_internal_details(
    application: FastAPI, query_session: Mock, path: str
) -> None:
    error = OperationalError("SELECT private_plan_table", {}, RuntimeError("private-db-detail"))
    query_session.execute.side_effect = error
    query_session.scalar.side_effect = error
    query_session.scalars.side_effect = error
    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get(path)
    assert response.status_code == 500
    assert response.json() == {
        "code": "INTERNAL_SERVER_ERROR",
        "message": "서버에서 오류가 발생했습니다.",
        "data": None,
    }
    assert "private" not in response.text
    assert "SELECT" not in response.text


def test_query_openapi_documents_pagination_and_uuid(application: FastAPI) -> None:
    document = application.openapi()
    operation = document["paths"]["/api/v1/plans"]["get"]
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
    assert set(parameters) == {"limit", "offset", "sort", "q", "category", "favorite"}
    for name in parameters:
        assert parameters[name]["in"] == "query"
    for name in ("limit", "offset"):
        assert parameters[name]["schema"]["type"] == "integer"
    assert parameters["limit"]["schema"]["default"] == 20
    assert parameters["limit"]["schema"]["minimum"] == 1
    assert parameters["limit"]["schema"]["maximum"] == 100
    assert parameters["offset"]["schema"]["default"] == 0
    assert parameters["offset"]["schema"]["minimum"] == 0
    assert parameters["offset"]["schema"]["maximum"] == 9223372036854775807
    assert {"200", "422", "500"} <= set(operation["responses"])

    operation = document["paths"]["/api/v1/plans/{plan_id}"]["get"]
    assert operation["parameters"][0]["schema"]["format"] == "uuid"
    assert {"200", "404", "422", "500"} <= set(operation["responses"])


def test_summary_time_is_utc_and_totals_are_integers() -> None:
    summary = PlanSummary(
        plan_id=uuid4(),
        vehicle_id="avante_demo",
        vehicle_name="아반떼 데모",
        passenger_count=2,
        item_type_count=2,
        item_total_quantity=5,
        created_at=datetime(2026, 11, 11, 12, tzinfo=timezone(timedelta(hours=9))),
    )
    assert summary.created_at == datetime(2026, 11, 11, 3, tzinfo=UTC)
    data = summary.model_dump(mode="json")
    assert data["created_at"].endswith("Z")
    assert data["item_type_count"] == 2
    assert data["item_total_quantity"] == 5
    assert data["latest_calculation_id"] is None
