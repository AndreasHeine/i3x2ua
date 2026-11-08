"""
Application service factory and dependency injection.

Provides FastAPI dependency functions for service access in routers. The
model-build dependency itself lives in ``application.services.model_cache`` and
is re-exported here so bootstrap and routes share one implementation.
"""

from __future__ import annotations

from typing import cast

from fastapi import Depends, Request

from i3x_server.application.ports.subscription import SubscriptionServicePort
from i3x_server.application.services.model_cache import get_model_builder, get_or_build_model
from i3x_server.application.services.model_query import ModelQueryService
from i3x_server.application.services.object_value import ObjectValueService
from i3x_server.application.services.subscription import SubscriptionAppService
from i3x_server.domain.ports.opcua import OpcUaClientProtocol
from i3x_server.errors import i3x_http_error
from i3x_server.schemas.state import BuildResult

__all__ = [
    "get_model_builder",
    "get_object_value_service",
    "get_model_query_service",
    "get_opcua_client",
    "get_or_build_model",
    "get_subscription_app_service",
    "get_subscription_service",
]


def get_opcua_client(request: Request) -> OpcUaClientProtocol:
    client = cast(OpcUaClientProtocol | None, getattr(request.app.state, "opcua_client", None))
    if client is None:
        raise i3x_http_error(500, "InternalError", "OPC UA client not initialized")
    return client


def get_subscription_service(request: Request) -> SubscriptionServicePort:
    service = cast(SubscriptionServicePort | None, getattr(request.app.state, "subscription_service", None))
    if service is None:
        raise i3x_http_error(500, "InternalError", "Subscription service not initialized")
    return service


async def get_object_value_service(
    request: Request,
    model: BuildResult = Depends(get_or_build_model),
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> ObjectValueService:
    """Dependency function for ObjectValueService.

    Args:
        request: FastAPI request
        model: Built model structure
        opcua_client: OPC UA client

    Returns:
        ObjectValueService instance
    """
    return ObjectValueService(opcua_client=opcua_client, model=model, request=request)


async def get_model_query_service(
    model: BuildResult = Depends(get_or_build_model),
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> ModelQueryService:
    """Dependency function for ModelQueryService."""
    return ModelQueryService(opcua_client=opcua_client, model=model)


async def get_subscription_app_service(
    request: Request,
    model: BuildResult = Depends(get_or_build_model),
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
    subscription_service: SubscriptionServicePort = Depends(get_subscription_service),
) -> SubscriptionAppService:
    """Dependency function for SubscriptionAppService.

    Args:
        request: FastAPI request
        model: Built model structure
        opcua_client: OPC UA client
        subscription_service: Low-level subscription service

    Returns:
        SubscriptionAppService instance
    """
    return SubscriptionAppService(
        opcua_client=opcua_client,
        model=model,
        subscription_service=subscription_service,
        request=request,
    )
