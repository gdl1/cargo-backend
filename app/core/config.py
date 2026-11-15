from typing import Annotated, Literal

from pydantic import Field, SecretStr, StringConstraints, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Chagokchagok Backend"
    app_env: Literal["local", "test", "production"] = "local"
    demo_user_id: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ] = "demo-user"
    postgres_host: str = "127.0.0.1"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_user: str = "chagokchagok"
    postgres_password: SecretStr
    postgres_db: str = "chagokchagok"
    db_connect_timeout: int = Field(default=3, ge=1, le=30)
    db_statement_timeout_ms: int = Field(default=5000, ge=100, le=60000)
    calculation_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    calculation_max_candidates: int = Field(default=20, ge=1, le=100)
    preview_ttl_seconds: int = Field(default=600, ge=30, le=86400)
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-6.1-terra"
    llm_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    llm_max_output_tokens: int = Field(default=4000, ge=256, le=16000)
    item_photo_s3_bucket: str = ""
    item_photo_s3_region: str = ""
    notification_api_key: SecretStr | None = None
    firebase_project_id: str = ""
    google_application_credentials: str = ""
    push_http_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    push_retry_delay_seconds: int = Field(default=60, ge=60, le=3600)
    notification_event_max_age_seconds: int = Field(default=600, ge=60, le=3600)

    @field_validator("notification_api_key")
    @classmethod
    def notification_key_must_be_strong(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or value.get_secret_value() == "":
            return None
        secret = value.get_secret_value()
        if len(secret) < 32 or not secret.isascii() or any(c.isspace() for c in secret):
            raise ValueError("NOTIFICATION_API_KEY는 공백 없는 ASCII 32자 이상이어야 합니다.")
        return value

    @field_validator("firebase_project_id")
    @classmethod
    def firebase_project_id_must_be_safe(cls, value: str) -> str:
        import re

        if value and not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", value):
            raise ValueError("Firebase 프로젝트 ID 형식을 확인해 주세요.")
        return value

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
