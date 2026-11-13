import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema

from alembic import command
from app.core.config import Settings
from app.db.session import build_engine
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
        openai_api_key=None,
        notification_api_key=None,
        firebase_project_id="",
        google_application_credentials="",
    )


@pytest.fixture
def application(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def model_session() -> Iterator[Session]:
    if os.getenv("RUN_DB_TESTS") != "1":
        pytest.skip("실제 DB 검사는 RUN_DB_TESTS=1일 때 실행합니다.")

    engine = build_engine(Settings(_env_file=None))
    schema = f"test_models_{uuid4().hex}"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            original_schema = connection.dialect.default_schema_name
            try:
                connection.execute(CreateSchema(schema))
                connection.execute(
                    text("SELECT set_config('search_path', :schema, true)"), {"schema": schema}
                )
                connection.dialect.default_schema_name = schema
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
                with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                    session.info["migration_config"] = config
                    yield session
            finally:
                transaction.rollback()
                connection.dialect.default_schema_name = original_schema
    finally:
        engine.dispose()
