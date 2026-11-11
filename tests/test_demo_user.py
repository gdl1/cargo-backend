from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.dependencies import get_current_user_id
from app.core.config import Settings
from app.main import create_app


def user_app(settings: Settings) -> FastAPI:
    application = create_app(settings)

    @application.post("/test-user")
    def current_user(user_id: Annotated[str, Depends(get_current_user_id)]) -> dict[str, str]:
        return {"user_id": user_id}

    return application


def test_independent_clients_use_same_demo_user(settings: Settings) -> None:
    with TestClient(user_app(settings)) as mobile, TestClient(user_app(settings)) as pleos:
        for client in (mobile, pleos):
            response = client.post("/test-user")
            assert response.status_code == 200
            assert response.json() == {"user_id": settings.demo_user_id}


def test_request_cannot_override_demo_user(settings: Settings) -> None:
    with TestClient(user_app(settings)) as client:
        response = client.post(
            "/test-user",
            params={"user_id": "query-user"},
            headers={"user_id": "header-user", "X-User-ID": "other-user"},
            json={"user_id": "body-user"},
        )
        assert response.status_code == 200
        assert response.json() == {"user_id": settings.demo_user_id}


def test_settings_do_not_leak_between_applications(settings: Settings) -> None:
    first_settings = settings.model_copy(update={"demo_user_id": "first-demo"})
    second_settings = settings.model_copy(update={"demo_user_id": "second-demo"})
    with (
        TestClient(user_app(first_settings)) as first,
        TestClient(user_app(second_settings)) as second,
    ):
        assert first.post("/test-user").json() == {"user_id": "first-demo"}
        assert second.post("/test-user").json() == {"user_id": "second-demo"}
        assert first.post("/test-user").json() == {"user_id": "first-demo"}


def test_default_demo_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEMO_USER_ID", raising=False)
    settings = Settings(_env_file=None, postgres_password="test-only")
    assert settings.demo_user_id == "demo-user"


def test_demo_user_id_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_USER_ID", "  shared-demo  ")
    settings = Settings(_env_file=None, postgres_password="test-only")
    with TestClient(user_app(settings)) as client:
        assert client.post("/test-user").json() == {"user_id": "shared-demo"}


@pytest.mark.parametrize("user_id", ["", "   ", "\t\n", "x" * 101])
def test_invalid_demo_user_id_rejected(user_id: str) -> None:
    with pytest.raises(ValidationError, match="demo_user_id"):
        Settings(_env_file=None, postgres_password="test-only", demo_user_id=user_id)
