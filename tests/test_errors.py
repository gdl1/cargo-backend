import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field


class ExamplePayload(BaseModel):
    quantity: int = Field(gt=0)


def test_unknown_route_uses_common_error_response(application: FastAPI) -> None:
    with TestClient(application) as client:
        response = client.get("/does-not-exist")

    assert response.status_code == 404
    assert response.json() == {"code": "HTTP_404", "message": "Not Found", "data": None}


def test_http_exception_preserves_status_and_headers(application: FastAPI) -> None:
    @application.get("/test-auth")
    def protected_route() -> None:
        raise HTTPException(
            status_code=401,
            detail="인증이 필요합니다.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    with TestClient(application) as client:
        response = client.get("/test-auth")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["code"] == "HTTP_401"


@pytest.mark.parametrize("quantity", ["private-input", 0])
def test_validation_error_omits_input_and_internal_context(
    application: FastAPI, quantity: str | int
) -> None:
    @application.post("/test-validation")
    def validate_payload(payload: ExamplePayload) -> ExamplePayload:
        return payload

    with TestClient(application) as client:
        response = client.post("/test-validation", json={"quantity": quantity})

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    error = response.json()["data"]["errors"][0]
    assert error["loc"] == ["body", "quantity"]
    assert "input" not in error
    assert "ctx" not in error
    assert "private-input" not in response.text


def test_unexpected_error_hides_internal_exception(application: FastAPI) -> None:
    @application.get("/test-error")
    def failing_route() -> None:
        raise RuntimeError("private-internal-connection-detail")

    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get("/test-error")

    assert response.status_code == 500
    assert response.json() == {
        "code": "INTERNAL_SERVER_ERROR",
        "message": "서버에서 오류가 발생했습니다.",
        "data": None,
    }
    assert "private-internal" not in response.text


def test_method_not_allowed_preserves_allow_header(application: FastAPI) -> None:
    with TestClient(application) as client:
        response = client.post("/api/v1/health")

    assert response.status_code == 405
    assert response.json()["code"] == "HTTP_405"
    assert "GET" in response.headers["allow"]
