from __future__ import annotations

import asyncio
import http
import logging
import os
import re
import signal
import types
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from time import perf_counter

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

from i3x_server.api.mcp import router as mcp_router
from i3x_server.api.ua import router as ua_router
from i3x_server.api.v1 import router as v1_router
from i3x_server.application.errors import ApplicationServiceError
from i3x_server.config.settings import settings
from i3x_server.infrastructure.opcua.client import OpcUaClient
from i3x_server.infrastructure.subscriptions.service import SubscriptionService
from i3x_server.mcp import build_mcp_tools, get_api_prefix, init_mcp_metrics, load_prompt_overrides
from i3x_server.model.builder import ModelBuilder
from i3x_server.prompts.registry import PromptRegistry
from i3x_server.schemas.state import BuildResult

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _status_title(status_code: int) -> str:
    try:
        return http.HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _to_lower_camel_case(value: str) -> str:
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", value) if part]
    if not parts:
        return "operation"
    return parts[0].lower() + "".join(part[:1].upper() + part[1:] for part in parts[1:])


def _readable_operation_id(route: APIRoute) -> str:
    route_name = str(getattr(route, "name", "") or "")
    candidate = _to_lower_camel_case(route_name)
    if candidate != "operation":
        return candidate

    methods = sorted(method for method in (route.methods or set()) if method not in {"HEAD", "OPTIONS"})
    method = methods[0].lower() if methods else "call"
    path_hint = route.path_format.strip("/").replace("/", "_").replace("{", "").replace("}", "")
    return _to_lower_camel_case(f"{method}_{path_hint}")


def _configure_logging() -> None:
    level_name = settings.log_level.upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("asyncua").setLevel(logging.WARNING)


def _configure_otel(app: FastAPI) -> None:
    if not settings.otel_enabled:
        return

    try:
        from opentelemetry import metrics as otel_metrics
        from opentelemetry import trace as otel_trace
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "opentelemetry-sdk not installed; tracing disabled. Install the 'otel' extras: pip install 'i3x2ua[otel]'."
        )
        return

    resource = Resource.create({SERVICE_NAME: settings.otel_service_name})
    tracer_provider = TracerProvider(resource=resource)
    otlp_endpoint = settings.otel_otlp_endpoint

    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            tracer_provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/traces"))
            )
            logger.info("OpenTelemetry OTLP trace exporter configured endpoint=%s", otlp_endpoint)
        except ImportError:
            logger.warning("opentelemetry-exporter-otlp-proto-http not installed; OTLP trace exporter skipped.")
    else:
        logger.info("OpenTelemetry enabled but I3X_OTEL_OTLP_ENDPOINT not set; spans will not be exported.")

    otel_trace.set_tracer_provider(tracer_provider)

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
        logger.info("FastAPI OpenTelemetry instrumentation enabled")
    except ImportError:
        logger.warning("opentelemetry-instrumentation-fastapi not installed; FastAPI auto-instrumentation skipped.")

    try:
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    except ImportError:
        logger.info("OpenTelemetry configured (traces only) service=%s", settings.otel_service_name)
        return

    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

            reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/metrics")
            )
            meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
            otel_metrics.set_meter_provider(meter_provider)
            logger.info("OpenTelemetry OTLP metric exporter configured endpoint=%s", otlp_endpoint)
        except ImportError:
            logger.warning("opentelemetry-exporter-otlp-proto-http not installed; OTLP metric exporter skipped.")

    init_mcp_metrics()
    logger.info("OpenTelemetry configured service=%s", settings.otel_service_name)


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
        connection_monitor_interval_seconds=settings.opcua_connection_monitor_interval_seconds,
    )
    skip_connect = os.getenv("I3X_SKIP_OPCUA_CONNECT", "0") == "1"
    logger.info(
        "App startup opcua_endpoint=%s skip_connect=%s log_level=%s "
        "browse_concurrency=%d metadata_cache_ttl_seconds=%d connection_monitor_interval_seconds=%d "
        "auth_configured=%s security_mode=%s mcp_enabled=%s",
        settings.opcua_endpoint,
        skip_connect,
        settings.log_level,
        settings.opcua_browse_concurrency,
        settings.opcua_metadata_cache_ttl_seconds,
        settings.opcua_connection_monitor_interval_seconds,
        bool(settings.opcua_username and settings.opcua_password),
        settings.opcua_security_mode,
        mcp_enabled,
    )
    if not skip_connect:
        await opcua_client.connect()
    app.state.opcua_client = opcua_client
    app.state.model_builder = ModelBuilder(opcua_client)
    app.state.object_type_context_cache = None
    app.state.object_type_lock = asyncio.Lock()
    app.state.subscription_service = SubscriptionService(
        opcua_client=opcua_client,
        interval_seconds=settings.subscription_interval_seconds,
        max_updates_per_subscription=settings.subscription_max_updates,
        ttl_seconds=settings.subscription_ttl_seconds,
        seed_initial_values=settings.subscriptions_initial_values,
        native_timeout_refresh_mode=settings.subscription_native_timeout_refresh_mode,
        native_timeout_refresh_keepalives=settings.subscription_native_timeout_refresh_keepalives,
        native_timeout_refresh_max_seconds=settings.subscription_native_timeout_refresh_max_seconds,
    )
    # Install a chained signal handler so that active SSE streaming connections
    # are closed *before* Uvicorn's "Waiting for connections to close" phase.
    # On Windows (and as a fallback on Unix), Uvicorn uses signal.signal(), so we
    # save the existing handler and wrap it.  On Unix, asyncio's add_signal_handler
    # is tried first so that the event-loop callback is already on the right thread.
    _sub_svc = app.state.subscription_service
    _loop = asyncio.get_event_loop()

    def _early_shutdown() -> None:
        """Called from the asyncio event loop when a termination signal fires."""
        _sub_svc.initiate_shutdown()

    def _make_chained(sig: int) -> None:
        _old = signal.getsignal(sig)

        def _handler(signum: int, frame: types.FrameType | None) -> None:
            _loop.call_soon_threadsafe(_early_shutdown)
            if callable(_old):
                _old(signum, frame)

        signal.signal(sig, _handler)

    _handled_signals = [signal.SIGINT]
    if hasattr(signal, "SIGTERM"):
        _handled_signals.append(signal.SIGTERM)
    for _sig in _handled_signals:
        try:
            # On Unix, prefer asyncio's loop handler so we stay on the event loop thread.
            _loop.add_signal_handler(_sig, _early_shutdown)
        except (NotImplementedError, OSError, RuntimeError):
            # Windows: asyncio does not support add_signal_handler; chain with signal.signal.
            try:
                _make_chained(_sig)
            except (ValueError, OSError):
                # signal.signal() only works from the main thread.
                # In test environments the lifespan runs in a worker thread, so skip
                # signal registration silently rather than crashing.
                pass

    app.state.model_lock = asyncio.Lock()
    app.state.model_preload_task = None
    if mcp_enabled:
        openapi_spec = app.openapi()
        if not isinstance(openapi_spec, dict):
            raise ValueError(f"OpenAPI spec must be dict, got {type(openapi_spec)}")

        app.state.mcp_tools = build_mcp_tools(openapi_spec)
        app.state.mcp_api_prefix = get_api_prefix(openapi_spec)
        prompt_overrides = load_prompt_overrides()
        app.state.mcp_prompts = PromptRegistry.load_from_overrides(
            prompt_overrides,
        )
        logger.info(
            "MCP prompt registry loaded from overrides count=%d",
            len(app.state.mcp_prompts.list_metadata()),
        )
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
        "Turn any OPC UA server into an i3X-compliant REST and MCP Enabled API with OpenAPI docs, "
        "JSON, and live SSE streams. No OPC UA expertise required."
    )
    app = FastAPI(
        title="The i3X API Gateway for OPC UA",
        version="1.4",
        description=description,
        lifespan=lifespan,
        generate_unique_id_function=_readable_operation_id,
    )
    app.add_middleware(GZipMiddleware, minimum_size=1)
    cors_origins = settings.cors_allowed_origins
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "Accept", "Authorization"],
            allow_credentials=False,
        )

    # Keep FastAPI's native OpenAPI generator as the single source of truth.

    project_root = PROJECT_ROOT
    static_dir = project_root / "static"
    # Backward-compatible fallback for local setups that still use the legacy img folder.
    if not static_dir.exists():
        static_dir = project_root / "img"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

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

    @app.exception_handler(ApplicationServiceError)
    async def handle_application_service_error(
        request: Request,
        exc: ApplicationServiceError,
    ) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "success": False,
                "error": {"code": int(exc.status_code), "message": exc.message},
                "responseDetail": {
                    "title": _status_title(int(exc.status_code)),
                    "status": int(exc.status_code),
                    "detail": exc.message,
                },
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
    async def add_security_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if response.headers.get("content-type", "").startswith("text/html"):
            is_docs_route = request.url.path.startswith("/docs") or request.url.path.startswith("/redoc")
            csp_value = (
                "default-src 'self'; img-src 'self' data: https://fastapi.tiangolo.com; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
                if is_docs_route
                else "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'"
            )
            response.headers.setdefault(
                "Content-Security-Policy",
                csp_value,
            )
        return response

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
    app.include_router(ua_router)
    if mcp_enabled:
        app.include_router(mcp_router)

    frontend_dist = project_root / "dist"
    if frontend_dist.exists():
        app.frontend("/", directory=str(frontend_dist), fallback=None)
    else:
        logger.warning("Frontend dist directory not found at %s; UI pages are unavailable", frontend_dist)

    _configure_otel(app)
    return app
