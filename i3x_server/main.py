from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from i3x_server.api.action import router as action_router
from i3x_server.api.data import router as data_router
from i3x_server.api.model import router as model_router
from i3x_server.config.settings import settings
from i3x_server.model.builder import ModelBuilder
from i3x_server.opcua.client import OpcUaClient


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    opcua_client = OpcUaClient(endpoint=settings.opcua_endpoint)
    skip_connect = os.getenv("I3X_SKIP_OPCUA_CONNECT", "0") == "1"
    if not skip_connect:
        await opcua_client.connect()
    app.state.opcua_client = opcua_client
    app.state.model_builder = ModelBuilder(opcua_client)
    app.state.model_cache = None
    app.state.model_lock = asyncio.Lock()
    try:
        yield
    finally:
        if not skip_connect:
            await opcua_client.disconnect()


def create_app() -> FastAPI:
    app = FastAPI(title="i3X OPC UA Provider", version="0.1.0", lifespan=lifespan)
    app.include_router(model_router)
    app.include_router(data_router)
    app.include_router(action_router)
    return app


app = create_app()
