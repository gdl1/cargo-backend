"""애플리케이션과 같은 설정 및 메타데이터를 사용하는 마이그레이션 환경."""

from logging.config import fileConfig

import app.models  # noqa: F401 -- 후속 모델을 메타데이터에 등록합니다.
from alembic import context
from app.core.config import Settings
from app.db.base import Base
from app.db.session import build_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """DB 연결 없이 SQL을 생성합니다."""
    context.configure(
        url=Settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """URL을 ini에 문자열로 넣지 않고 엔진에 직접 전달합니다."""
    engine = build_engine(Settings())
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
