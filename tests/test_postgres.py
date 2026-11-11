import os

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1", reason="실제 DB 검사는 RUN_DB_TESTS=1일 때 실행합니다."
)
def test_health_with_real_postgresql() -> None:
    # CI 또는 테스트용 환경변수만 사용합니다. 개발자의 .env는 읽지 않습니다.
    settings = Settings(_env_file=None)

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/health/db")

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"status": "ok", "database": "ok"}
