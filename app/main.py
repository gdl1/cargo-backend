from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker
from starlette.concurrency import run_in_threadpool

from app.api.router import api_router
from app.core.config import Settings
from app.core.exceptions import register_exception_handlers
from app.db.session import build_engine


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        config = settings if settings is not None else Settings()
        engine = build_engine(config)
        application.state.settings = config
        application.state.engine = engine
        application.state.session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        try:
            yield
        finally:
            await run_in_threadpool(engine.dispose)

    application = FastAPI(
        title="Cargo Backend",
        version="0.1.0",
        description="적재 계획 백엔드의 기본 실행 환경",
        lifespan=lifespan,
    )
    register_exception_handlers(application)
    application.include_router(api_router)
    return application


app = create_app()
