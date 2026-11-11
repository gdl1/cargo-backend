from importlib import import_module
from unittest.mock import Mock

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.session import build_engine
from app.main import create_app


def test_engine_is_disposed_on_application_shutdown(settings: Settings, monkeypatch) -> None:
    engine = build_engine(settings)
    dispose = Mock(wraps=engine.dispose)
    monkeypatch.setattr(engine, "dispose", dispose)
    monkeypatch.setattr(import_module("app.main"), "build_engine", lambda config: engine)

    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/health").status_code == 200
        dispose.assert_not_called()

    dispose.assert_called_once_with()
