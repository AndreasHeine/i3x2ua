from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from time import perf_counter

from fastapi import FastAPI, Request, Response

from i3x_server.api.beta import router as beta_router
from i3x_server.config.settings import settings
from i3x_server.model.builder import ModelBuilder
from i3x_server.opcua.client import OpcUaClient
from i3x_server.schemas.state import BuildResult
from i3x_server.subscriptions.service import SubscriptionService

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    level_name = settings.log_level.upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


async def _run_model_preload(app: FastAPI) -> None:
    try:
        started = asyncio.get_running_loop().time()
        preload = await app.state.model_builder.build()
        app.state.model_cache = preload
        logger.info(
            "Model preload finished nodes=%d roots=%d properties=%d actions=%d duration_s=%.3f",
            len(preload.nodes_by_id),
            len(preload.root_ids),
            len(preload.property_to_node),
            len(preload.action_to_method),
            asyncio.get_running_loop().time() - started,
        )
    except Exception:
        logger.exception("Model preload failed")
        if settings.fail_startup_on_model_preload_error and settings.model_preload_blocking:
            raise
        logger.warning("Continuing without preloaded model; model will build lazily on demand")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _configure_logging()
    opcua_client = OpcUaClient(
        endpoint=settings.opcua_endpoint,
        username=settings.opcua_username,
        password=settings.opcua_password,
        security_mode=settings.opcua_security_mode,
        security_policy=settings.opcua_security_policy,
        client_cert_path=settings.opcua_client_cert_path,
        client_key_path=settings.opcua_client_key_path,
        client_key_password=settings.opcua_client_key_password,
        server_cert_path=settings.opcua_server_cert_path,
        browse_concurrency=settings.opcua_browse_concurrency,
        metadata_cache_ttl_seconds=settings.opcua_metadata_cache_ttl_seconds,
    )
    skip_connect = os.getenv("I3X_SKIP_OPCUA_CONNECT", "0") == "1"
    logger.info(
        "App startup opcua_endpoint=%s skip_connect=%s log_level=%s "
        "browse_concurrency=%d metadata_cache_ttl_seconds=%d auth_configured=%s security_mode=%s",
        settings.opcua_endpoint,
        skip_connect,
        settings.log_level,
        settings.opcua_browse_concurrency,
        settings.opcua_metadata_cache_ttl_seconds,
        bool(settings.opcua_username and settings.opcua_password),
        settings.opcua_security_mode,
    )
    if not skip_connect:
        await opcua_client.connect()
    app.state.opcua_client = opcua_client
    app.state.model_builder = ModelBuilder(opcua_client)
    app.state.subscription_service = SubscriptionService(
        opcua_client=opcua_client,
        interval_seconds=settings.subscription_interval_seconds,
    )
    app.state.model_lock = asyncio.Lock()
    app.state.model_preload_task = None
    if skip_connect:
        app.state.model_cache = BuildResult(
            nodes_by_id={},
            root_ids=[],
            children_by_id={},
            property_to_node={},
            action_to_method={},
        )
    else:
        app.state.model_cache = None
        if settings.model_preload_on_startup:
            if settings.model_preload_blocking:
                logger.info("Model preload at startup enabled (blocking)")
                await _run_model_preload(app)
            else:
                logger.info("Model preload at startup enabled (background)")
                app.state.model_preload_task = asyncio.create_task(_run_model_preload(app))
    try:
        yield
    finally:
        logger.info("App shutdown started")
        preload_task = getattr(app.state, "model_preload_task", None)
        if preload_task is not None and not preload_task.done():
            preload_task.cancel()
            with suppress(asyncio.CancelledError):
                await preload_task
        await app.state.subscription_service.close()
        if not skip_connect:
            await opcua_client.disconnect()
        logger.info("App shutdown finished")


def create_app() -> FastAPI:
    app = FastAPI(title="i3X API Beta", version="beta", lifespan=lifespan)

    openapi_doc_path = Path(__file__).resolve().parents[1] / "openapi.json"
    openapi_override: dict[str, object] | None = None

    def custom_openapi() -> dict[str, object]:
        nonlocal openapi_override
        if openapi_override is None:
            openapi_override = json.loads(openapi_doc_path.read_text(encoding="utf-8"))
        return openapi_override

    app.openapi = custom_openapi  # type: ignore[method-assign]

    @app.middleware("http")
    async def log_http_requests(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started = perf_counter()
        response = await call_next(request)
        logger.info(
            "HTTP request method=%s path=%s status=%s duration_s=%.3f",
            request.method,
            request.url.path,
            response.status_code,
            perf_counter() - started,
        )
        return response

    app.include_router(beta_router)
    return app


app = create_app()
