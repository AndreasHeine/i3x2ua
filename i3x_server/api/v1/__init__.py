"""V1 API router aggregator."""

from fastapi import APIRouter

from i3x_server.api.v1.core_routes import router as core_router
from i3x_server.api.v1.model_routes import router as model_router
from i3x_server.api.v1.monolithic import router as monolithic_router
from i3x_server.api.v1.object_routes import router as object_router
from i3x_server.api.v1.object_value_routes import router as object_value_router
from i3x_server.api.v1.objecttype_routes import router as objecttype_router
from i3x_server.api.v1.subscription_routes import router as subscription_router

router = APIRouter()
router.include_router(core_router)
router.include_router(model_router)
router.include_router(object_router)
router.include_router(objecttype_router)
router.include_router(object_value_router)
router.include_router(subscription_router)
router.include_router(monolithic_router)

__all__ = ["router"]
