from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Cargo Backend"
    app_env: Literal["local", "test", "production"] = "local"
    postgres_host: str = "127.0.0.1"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_user: str = "cargo"
    postgres_password: SecretStr
    postgres_db: str = "cargo"
    db_connect_timeout: int = Field(default=3, ge=1, le=30)
    db_statement_timeout_ms: int = Field(default=5000, ge=100, le=60000)

    @field_validator("postgres_password")
    @classmethod
    def password_must_not_be_empty(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("POSTGRES_PASSWORD를 설정해 주세요.")
        return value

    @property
    def database_url(self) -> URL:
        # URL 객체를 사용하면 비밀번호의 @, %, / 등을 직접 이스케이프할 필요가 없습니다.
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )
