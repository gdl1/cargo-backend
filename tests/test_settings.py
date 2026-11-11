import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy.engine import make_url

from app.core.config import Settings


def test_database_password_special_characters_survive_url_round_trip(settings: Settings) -> None:
    password = "example@pass%word:/?#"
    settings.postgres_password = SecretStr(password)

    reconstructed = make_url(settings.database_url.render_as_string(hide_password=False))

    assert reconstructed.password == password
    assert reconstructed.drivername == "postgresql+psycopg"
    assert password not in repr(settings)
    assert password not in str(settings.database_url)


def test_database_password_is_required(monkeypatch) -> None:
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)

    with pytest.raises(ValidationError, match="postgres_password"):
        Settings(_env_file=None)


def test_empty_database_password_is_rejected() -> None:
    with pytest.raises(ValidationError, match="POSTGRES_PASSWORD"):
        Settings(_env_file=None, postgres_password=SecretStr(""))


@pytest.mark.parametrize("port", [0, 65536])
def test_invalid_database_port_is_rejected(port: int) -> None:
    with pytest.raises(ValidationError, match="postgres_port"):
        Settings(_env_file=None, postgres_password=SecretStr("test_only"), postgres_port=port)


def test_environment_overrides_dotenv(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POSTGRES_PASSWORD=dotenv_only\nPOSTGRES_DB=dotenv_database\n", encoding="utf-8"
    )
    monkeypatch.setenv("POSTGRES_DB", "environment_database")
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)

    configured = Settings(_env_file=env_file)

    assert configured.postgres_db == "environment_database"
    assert configured.postgres_password.get_secret_value() == "dotenv_only"
