from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker
from starlette.concurrency import run_in_threadpool

from app.api.router import api_router
from app.core.config import Settings
from app.core.exceptions import register_exception_handlers
from app.db.session import build_engine
from app.services.item_photos import ItemPhotoStorage
from app.services.push import PushSender
from app.services.vehicles import get_vehicle_catalog


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        config = settings if settings is not None else Settings()
        get_vehicle_catalog()
        engine = build_engine(config)
        application.state.settings = config
        application.state.item_photo_storage = ItemPhotoStorage(
            config.item_photo_s3_bucket, config.item_photo_s3_region
        )
        application.state.push_sender = PushSender(config)
        application.state.engine = engine
        application.state.session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        try:
            yield
        finally:
            await run_in_threadpool(engine.dispose)

    application = FastAPI(
        title="Chagokchagok Backend",
        version="0.1.0",
        description="차량 기준 데이터와 적재 계획 백엔드",
        lifespan=lifespan,
    )
    register_exception_handlers(application)
    application.include_router(api_router)
    return application


app = create_app()
