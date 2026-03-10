from __future__ import annotations

import asyncio
import logging
from time import perf_counter
from typing import cast

from fastapi import Request

from i3x_server.errors import i3x_http_error
from i3x_server.model.builder import ModelBuilder
from i3x_server.opcua.client import OpcUaClientProtocol
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


async def get_or_build_model(request: Request) -> BuildResult:
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
        logger.info(
            "Model build finished nodes=%d roots=%d properties=%d actions=%d duration_s=%.3f",
            len(built.nodes_by_id),
            len(built.root_ids),
            len(built.property_to_node),
            len(built.action_to_method),
            perf_counter() - started,
        )
        return built
