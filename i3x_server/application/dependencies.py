"""
Application service factory and dependency injection.

Provides FastAPI dependency functions for service access in routers.
"""

from __future__ import annotations

import asyncio
import logging
from time import perf_counter
from typing import cast

from fastapi import Depends, Request

from i3x_server.application.ports.subscription import SubscriptionServicePort
from i3x_server.application.services.mcp import McpService
from i3x_server.application.services.object_value import ObjectValueService
from i3x_server.application.services.subscription import SubscriptionAppService
from i3x_server.domain.ports.opcua import OpcUaClientProtocol
from i3x_server.errors import i3x_http_error
from i3x_server.model.builder import ModelBuilder
from i3x_server.schemas.state import BuildResult

logger = logging.getLogger(__name__)


def get_model_builder(request: Request) -> ModelBuilder:
    builder = cast(ModelBuilder | None, getattr(request.app.state, "model_builder", None))
    if builder is None:
        raise i3x_http_error(500, "InternalError", "ModelBuilder not initialized")
    return builder


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


async def get_or_build_model(request: Request) -> BuildResult:
    preload_task = cast(asyncio.Task[None] | None, getattr(request.app.state, "model_preload_task", None))
    if preload_task is not None and not preload_task.done():
        logger.info("Model preload in progress; waiting for completion")
        try:
            await preload_task
        except Exception:
            logger.exception("Background model preload failed; falling back to lazy build")

    cache = cast(BuildResult | None, getattr(request.app.state, "model_cache", None))
    if cache is not None:
        logger.debug("Model cache hit")
        return cache

    lock: asyncio.Lock = request.app.state.model_lock
    async with lock:
        cache = cast(BuildResult | None, getattr(request.app.state, "model_cache", None))
        if cache is not None:
            logger.debug("Model cache hit after lock")
            return cache
        builder = get_model_builder(request)
        started = perf_counter()
        logger.info("Model build started")
        built = await builder.build()
        request.app.state.model_cache = built
        request.app.state.object_type_context_cache = None
        logger.info(
            "Model build finished nodes=%d roots=%d properties=%d actions=%d duration_s=%.3f",
            len(built.nodes_by_id),
            len(built.root_ids),
            len(built.property_to_node),
            len(built.action_to_method),
            perf_counter() - started,
        )
        return built


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
