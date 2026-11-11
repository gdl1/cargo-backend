from collections.abc import Iterator
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.seed import seed_vehicles
from app.db.session import get_db
from app.main import create_app
from app.models import Plan, Vehicle

VEHICLE_SUMMARY = {
    "vehicle_id": "avante_demo",
    "name": "아반떼 데모",
    "seat_capacity": 5,
    "data_version": "avante-demo-v1",
    "data_source": "DUMMY",
}
VEHICLE_DETAIL = {
    **VEHICLE_SUMMARY,
    "seat_codes": [
        "ROW_1_LEFT",
        "ROW_1_RIGHT",
        "ROW_2_LEFT",
        "ROW_2_CENTER",
        "ROW_2_RIGHT",
    ],
    "driver_seat": "ROW_1_LEFT",
    "fold_segments": [],
    "packing_profiles": [
        {
            "target_folded_segments": [],
            "packing_space": {"width_cm": 97, "depth_cm": 82, "height_cm": 42},
        }
    ],
    "opening": {"width_cm": 99, "height_cm": 41.5},
}
NOT_FOUND = {"code": "VEHICLE_NOT_FOUND", "message": "차량을 찾을 수 없습니다.", "data": None}


def use_session(application: FastAPI, session: Session) -> None:
    def override_db() -> Iterator[Session]:
        yield session

    application.dependency_overrides[get_db] = override_db


@pytest.fixture
def vehicle_client(application: FastAPI, model_session: Session) -> Iterator[TestClient]:
    use_session(application, model_session)
    with TestClient(application) as client:
        yield client


@pytest.mark.integration
class TestVehicleDatabase:
    def test_seed_is_repeatable_and_preserves_references(self, model_session: Session) -> None:
        assert seed_vehicles(model_session) == 1
        assert seed_vehicles(model_session) == 1
        assert model_session.scalar(select(func.count()).select_from(Vehicle)) == 1

        vehicle = model_session.get(Vehicle, "avante_demo")
        vehicle.name = "이전 데모 이름"
        plan = Plan(user_id="demo-user", vehicle=vehicle, passenger_count=1)
        model_session.add_all([plan, Vehicle(vehicle_id="other_vehicle", name="기존 차량")])
        model_session.flush()
        plan_id = plan.plan_id

        seed_vehicles(model_session)
        model_session.flush()
        model_session.expire_all()
        assert model_session.scalar(select(func.count()).select_from(Vehicle)) == 2
        assert model_session.get(Vehicle, "other_vehicle").name == "기존 차량"
        assert model_session.get(Plan, plan_id).vehicle_id == "avante_demo"
        assert model_session.get(Plan, plan_id).vehicle.name == "아반떼 데모"

    def test_before_seed_returns_empty_list_and_not_found(self, vehicle_client: TestClient) -> None:
        response = vehicle_client.get("/api/v1/vehicles")
        assert response.status_code == 200
        assert response.json() == {
            "code": "SUCCESS",
            "message": "차량 목록을 조회했습니다.",
            "data": {"vehicles": []},
        }
        response = vehicle_client.get("/api/v1/vehicles/avante_demo")
        assert response.status_code == 404
        assert response.json() == NOT_FOUND

    def test_seeded_vehicle_responses(
        self, vehicle_client: TestClient, model_session: Session
    ) -> None:
        seed_vehicles(model_session)
        model_session.add(Vehicle(vehicle_id="other_vehicle", name="다른 차량"))
        model_session.flush()

        response = vehicle_client.get("/api/v1/vehicles")
        assert response.status_code == 200
        assert response.json() == {
            "code": "SUCCESS",
            "message": "차량 목록을 조회했습니다.",
            "data": {"vehicles": [VEHICLE_SUMMARY]},
        }
        response = vehicle_client.get("/api/v1/vehicles/avante_demo")
        assert response.status_code == 200
        assert response.json() == {
            "code": "SUCCESS",
            "message": "차량 정보를 조회했습니다.",
            "data": VEHICLE_DETAIL,
        }
        assert isinstance(response.json()["data"]["opening"]["height_cm"], float)
        for vehicle_id in ("unknown", "other_vehicle"):
            response = vehicle_client.get(f"/api/v1/vehicles/{vehicle_id}")
            assert response.status_code == 404
            assert response.json() == NOT_FOUND
        assert model_session.get(Vehicle, "other_vehicle") is not None

        vehicle = model_session.get(Vehicle, "avante_demo")
        vehicle.name = "  변경된 데모 차량  "
        model_session.flush()
        model_session.expire_all()

        list_response = vehicle_client.get("/api/v1/vehicles")
        detail_response = vehicle_client.get("/api/v1/vehicles/avante_demo")
        assert list_response.status_code == detail_response.status_code == 200
        assert list_response.json()["data"]["vehicles"][0]["name"] == "변경된 데모 차량"
        assert detail_response.json()["data"]["name"] == "변경된 데모 차량"

    def test_clients_receive_same_vehicle_data(
        self, settings: Settings, model_session: Session
    ) -> None:
        seed_vehicles(model_session)
        mobile_app = create_app(settings)
        pleos_app = create_app(settings)
        use_session(mobile_app, model_session)
        use_session(pleos_app, model_session)
        with TestClient(mobile_app) as mobile, TestClient(pleos_app) as pleos:
            for path in ("/api/v1/vehicles", "/api/v1/vehicles/avante_demo"):
                baseline = mobile.get(path)
                response = pleos.get(
                    path,
                    params={"user_id": "other-user", "vehicle_id": "other_vehicle"},
                    headers={"X-User-ID": "external-user", "X-Vehicle-ID": "other_vehicle"},
                )
                assert baseline.status_code == response.status_code == 200
                assert baseline.json() == response.json()


@pytest.mark.parametrize("path", ["/api/v1/vehicles", "/api/v1/vehicles/avante_demo"])
def test_database_error_uses_safe_common_response(application: FastAPI, path: str) -> None:
    session = Mock(spec=Session)
    error = OperationalError("SELECT vehicle", {}, RuntimeError("private-db-connection-detail"))
    session.scalars.side_effect = error
    session.get.side_effect = error
    use_session(application, session)
    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get(path)
    assert response.status_code == 500
    assert response.json() == {
        "code": "INTERNAL_SERVER_ERROR",
        "message": "서버에서 오류가 발생했습니다.",
        "data": None,
    }
    assert "private-db" not in response.text
    assert "SELECT" not in response.text


def test_vehicle_openapi_has_paths_and_numeric_dimensions(application: FastAPI) -> None:
    document = application.openapi()
    for path in ("/api/v1/vehicles", "/api/v1/vehicles/{vehicle_id}"):
        assert "get" in document["paths"][path]
        assert "200" in document["paths"][path]["get"]["responses"]
    assert "404" in document["paths"]["/api/v1/vehicles/{vehicle_id}"]["get"]["responses"]

    schemas = document["components"]["schemas"]
    for schema_name in ("PackingSpace", "Opening"):
        for dimension in schemas[schema_name]["properties"].values():
            assert dimension["type"] == "number"
