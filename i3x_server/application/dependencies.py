"""
Application service factory and dependency injection.

Provides FastAPI dependency functions for service access in routers.
"""

from __future__ import annotations

from fastapi import Depends, Request

from i3x_server.application.ports.subscription import SubscriptionServicePort
from i3x_server.application.services.mcp import McpService
from i3x_server.application.services.object_value import ObjectValueService
from i3x_server.application.services.subscription import SubscriptionAppService
from i3x_server.dependencies import get_opcua_client, get_or_build_model, get_subscription_service
from i3x_server.domain.ports.opcua import OpcUaClientProtocol
from i3x_server.schemas.state import BuildResult


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


def get_mcp_service(request: Request) -> McpService:
    """Dependency function for McpService.

    Args:
        request: FastAPI request

    Returns:
        McpService instance
    """
    from i3x_server.prompts.registry import PromptRegistry

    prompt_registry = getattr(request.app.state, "mcp_prompts", None)
    if not isinstance(prompt_registry, PromptRegistry):
        prompt_registry = None
    return McpService(request=request, prompt_registry=prompt_registry)
