from __future__ import annotations

import asyncio
import http
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from time import perf_counter

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

from i3x_server.api.mcp import router as mcp_router
from i3x_server.api.v1 import router as v1_router
from i3x_server.config.settings import settings
from i3x_server.mcp import build_mcp_tools, get_api_prefix
from i3x_server.model.builder import ModelBuilder
from i3x_server.opcua.client import OpcUaClient
from i3x_server.schemas.state import BuildResult
from i3x_server.subscriptions.service import SubscriptionService

logger = logging.getLogger(__name__)


def _status_title(status_code: int) -> str:
    try:
        return http.HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _configure_logging() -> None:
    level_name = settings.log_level.upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("asyncua").setLevel(logging.WARNING)


async def _run_model_preload(app: FastAPI) -> None:
    try:
        started = asyncio.get_running_loop().time()
        app.state.opcua_client.reset_runtime_metrics()
        build_started = asyncio.get_running_loop().time()
        preload = await app.state.model_builder.build()
        build_duration_s = asyncio.get_running_loop().time() - build_started
        app.state.model_cache = preload
        metrics = app.state.opcua_client.snapshot_runtime_metrics()
        duration_s = asyncio.get_running_loop().time() - started
        logger.info(
            "Model preload finished nodes=%d roots=%d properties=%d actions=%d build_s=%.3f total_s=%.3f",
            len(preload.nodes_by_id),
            len(preload.root_ids),
            len(preload.property_to_node),
            len(preload.action_to_method),
            build_duration_s,
            duration_s,
        )
        logger.info(
            "Model preload metrics duration_s=%.3f rpc_calls=%d browse_calls=%d browse_nodes=%d "
            "browse_initial_references=%d browse_next_calls=%d browse_next_references=%d "
            "read_calls=%d read_nodes=%d history_read_calls=%d history_read_nodes=%d method_calls=%d "
            "browse_tree_calls=%d browse_tree_nodes=%d namespace_reads=%d namespaces=%d "
            "namespace_info_builds=%d namespace_info_count=%d object_type_reads=%d object_type_count=%d",
            duration_s,
            metrics.browse_calls
            + metrics.browse_next_calls
            + metrics.read_calls
            + metrics.history_read_calls
            + metrics.method_calls,
            metrics.browse_calls,
            metrics.browse_nodes,
            metrics.browse_initial_references,
            metrics.browse_next_calls,
            metrics.browse_next_references,
            metrics.read_calls,
            metrics.read_nodes,
            metrics.history_read_calls,
            metrics.history_read_nodes,
            metrics.method_calls,
            metrics.browse_tree_calls,
            metrics.browse_tree_nodes_last,
            metrics.namespace_reads,
            metrics.namespace_count_last,
            metrics.namespace_info_builds,
            metrics.namespace_info_count_last,
            metrics.object_type_reads,
            metrics.object_type_count_last,
        )
        logger.info("Namespace metadata preload skipped at startup; loading on first request")
    except Exception:
        logger.exception("Model preload failed")
        if settings.fail_startup_on_model_preload_error and settings.model_preload_blocking:
            raise
        logger.warning("Continuing without preloaded model; model will build lazily on demand")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _configure_logging()
    mcp_enabled = _env_flag("I3X_ENABLE_MCP")
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
        "browse_concurrency=%d metadata_cache_ttl_seconds=%d auth_configured=%s security_mode=%s mcp_enabled=%s",
        settings.opcua_endpoint,
        skip_connect,
        settings.log_level,
        settings.opcua_browse_concurrency,
        settings.opcua_metadata_cache_ttl_seconds,
        bool(settings.opcua_username and settings.opcua_password),
        settings.opcua_security_mode,
        mcp_enabled,
    )
    if not skip_connect:
        await opcua_client.connect()
    app.state.opcua_client = opcua_client
    app.state.model_builder = ModelBuilder(opcua_client)
    app.state.object_type_context_cache = None
    app.state.subscription_service = SubscriptionService(
        opcua_client=opcua_client,
        interval_seconds=settings.subscription_interval_seconds,
        max_updates_per_subscription=settings.subscription_max_updates,
        ttl_seconds=settings.subscription_ttl_seconds,
    )
    app.state.model_lock = asyncio.Lock()
    app.state.model_preload_task = None
    if mcp_enabled:
        openapi_spec = app.openapi()
        app.state.mcp_tools = build_mcp_tools(openapi_spec)
        app.state.mcp_api_prefix = get_api_prefix(openapi_spec)
    if skip_connect:
        app.state.model_cache = BuildResult(
            nodes_by_id={},
            root_ids=[],
            children_by_id={},
            instances_by_type_id={},
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
    mcp_enabled = _env_flag("I3X_ENABLE_MCP")
    description = (
        "Industrial Information Interface eXchange API - 1.0. "
        "Scope: read/query/subscribe are implemented; update/write operations are optional "
        "and may return 501 Not Implemented. "
        "MCP endpoints are optional and available only when I3X_ENABLE_MCP=1."
    )
    app = FastAPI(title="i3X API 1.0", version="1.0", description=description, lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=1)

    openapi_doc_path = Path(__file__).resolve().parents[1] / "openapi.json"
    openapi_override: dict[str, object] | None = None

    def custom_openapi() -> dict[str, object]:
        nonlocal openapi_override
        if openapi_override is None:
            openapi_override = json.loads(openapi_doc_path.read_text(encoding="utf-8"))
            if not mcp_enabled:
                paths = openapi_override.get("paths")
                if isinstance(paths, dict):
                    openapi_override["paths"] = {
                        path: spec for path, spec in paths.items() if not path.startswith("/mcp")
                    }
        return openapi_override

    app.openapi = custom_openapi  # type: ignore[method-assign]

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        del request, exc
        message = "Invalid request payload or parameters"
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": {
                    "code": 400,
                    "message": message,
                },
                "responseDetail": {
                    "title": _status_title(400),
                    "status": 400,
                    "detail": message,
                },
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        del request
        detail = exc.detail
        response_detail: dict[str, object] | None = None
        if isinstance(detail, dict):
            error = detail.get("error")
            if isinstance(error, dict):
                message = str(error.get("message", "Request failed"))
            else:
                message = str(detail.get("message", "Request failed"))
            raw_response_detail = detail.get("responseDetail")
            if isinstance(raw_response_detail, dict):
                response_detail = {
                    "title": str(raw_response_detail.get("title", "Error")),
                    "status": int(raw_response_detail.get("status", exc.status_code)),
                    "detail": str(raw_response_detail.get("detail", message)),
                }
        else:
            message = str(detail) if detail else "Request failed"
        if response_detail is None:
            response_detail = {
                "title": _status_title(int(exc.status_code)),
                "status": int(exc.status_code),
                "detail": message,
            }
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "success": False,
                "error": {"code": int(exc.status_code), "message": message},
                "responseDetail": response_detail,
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception method=%s path=%s", request.method, request.url.path)
        message = "Internal server error"
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": {"code": 500, "message": message},
                "responseDetail": {
                    "title": _status_title(500),
                    "status": 500,
                    "detail": message,
                },
            },
        )

    @app.middleware("http")
    async def log_http_requests(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started = perf_counter()
        try:
            response = await call_next(request)
        except RuntimeError as exc:
            if str(exc) == "No response returned." and await request.is_disconnected():
                logger.info(
                    "HTTP request aborted by client method=%s path=%s duration_s=%.3f",
                    request.method,
                    request.url.path,
                    perf_counter() - started,
                )
                return Response(status_code=499)
            raise
        logger.info(
            "HTTP request method=%s path=%s status=%s duration_s=%.3f",
            request.method,
            request.url.path,
            response.status_code,
            perf_counter() - started,
        )
        return response

    app.include_router(v1_router)
    if mcp_enabled:
        app.include_router(mcp_router)
    return app


app = create_app()
