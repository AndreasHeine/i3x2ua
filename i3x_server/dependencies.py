from __future__ import annotations

import asyncio
from typing import cast

from fastapi import Request

from i3x_server.errors import i3x_http_error
from i3x_server.model.builder import ModelBuilder
from i3x_server.opcua.client import OpcUaClientProtocol
from i3x_server.schemas.state import BuildResult


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
        return cache

    lock: asyncio.Lock = request.app.state.model_lock
    async with lock:
        cache = cast(BuildResult | None, getattr(request.app.state, "model_cache", None))
        if cache is not None:
            return cache
        builder = get_model_builder(request)
        built = await builder.build()
        request.app.state.model_cache = built
        return built
