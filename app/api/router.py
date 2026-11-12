from fastapi import APIRouter

from app.api.v1.calculations import router as calculations_router
from app.api.v1.health import router as health_router
from app.api.v1.items import router as items_router
from app.api.v1.plans import router as plans_router
from app.api.v1.previews import router as previews_router
from app.api.v1.vehicles import router as vehicles_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health_router)
api_router.include_router(vehicles_router)
api_router.include_router(plans_router)
api_router.include_router(calculations_router)
api_router.include_router(previews_router)
api_router.include_router(items_router)
