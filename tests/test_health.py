from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db.session import get_db


def test_liveness_does_not_connect_to_database(application: FastAPI, monkeypatch) -> None:
    with TestClient(application) as client:
        connect = Mock(side_effect=AssertionError("생존 확인은 DB에 접속하면 안 됩니다."))
        monkeypatch.setattr(application.state.engine, "connect", connect)

        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["data"] == {"status": "ok"}
    connect.assert_not_called()


def test_database_health_checks_database(application: FastAPI) -> None:
    session = Mock(spec=Session)
    session.execute.return_value.scalar_one.return_value = 1
    application.dependency_overrides[get_db] = lambda: session

    with TestClient(application) as client:
        response = client.get("/api/v1/health/db")

    assert response.status_code == 200
    assert response.json()["data"] == {"status": "ok", "database": "ok"}
    session.execute.assert_called_once()
    assert str(session.execute.call_args.args[0]) == "SELECT 1"


def test_database_failure_returns_503_without_connection_details(application: FastAPI) -> None:
    session = Mock(spec=Session)
    session.execute.side_effect = OperationalError(
        "SELECT 1", {}, Exception("private-password-and-database-host")
    )
    application.dependency_overrides[get_db] = lambda: session

    with TestClient(application) as client:
        response = client.get("/api/v1/health/db")
        liveness = client.get("/api/v1/health")

    assert response.status_code == 503
    assert response.json() == {
        "code": "DATABASE_UNAVAILABLE",
        "message": "DB에 연결할 수 없습니다.",
        "data": None,
    }
    assert "private-password" not in response.text
    assert liveness.status_code == 200


def test_openapi_documents_health_routes_and_database_failure(application: FastAPI) -> None:
    with TestClient(application) as client:
        response = client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/v1/health" in paths
    assert "503" in paths["/api/v1/health/db"]["get"]["responses"]
