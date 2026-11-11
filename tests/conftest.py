import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    # 단위 검사는 개발자의 .env나 실제 DB에 의존하지 않습니다.
    return Settings(
        _env_file=None,
        app_env="test",
        postgres_host="127.0.0.1",
        postgres_port=5432,
        postgres_user="unit_test",
        postgres_password=SecretStr("unit_test_only"),
        postgres_db="cargo_unit_test",
        db_connect_timeout=1,
        db_statement_timeout_ms=1000,
    )


@pytest.fixture
def application(settings: Settings) -> FastAPI:
    return create_app(settings)
