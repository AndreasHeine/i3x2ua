from __future__ import annotations

import asyncio
import base64
import hashlib
import http
import logging
import os
import posixpath
import re
import signal
import types
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from time import perf_counter
from typing import Any, cast

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

from i3x_server.api.health import router as health_router
from i3x_server.api.mcp import router as mcp_router
from i3x_server.api.mcp.jsonrpc import shutdown_mcp_transport
from i3x_server.api.openapi import DocumentedFastAPI
from i3x_server.api.ua import router as ua_router
from i3x_server.api.v1 import router as v1_router
from i3x_server.api.v1.contracts import HttpErrorResponse
from i3x_server.api.v1.objecttype_helpers import warm_object_type_context
from i3x_server.application.errors import ApplicationServiceError
from i3x_server.application.services.model_query import NamespaceSnapshotCache
from i3x_server.bootstrap.dependencies import get_or_build_model
from i3x_server.config.settings import Settings, get_settings, settings
from i3x_server.errors import format_error_details
from i3x_server.http_caching import MODEL_REVISION_HEADER, ModelRevisionMiddleware, NotModifiedError
from i3x_server.infrastructure.opcua.client import OpcUaClient, model_refresh_budget
from i3x_server.infrastructure.subscriptions.service import SubscriptionService
from i3x_server.mcp.invocation import get_api_prefix, init_mcp_metrics
from i3x_server.mcp.tool_definitions import build_mcp_tools, load_overrides, load_prompt_overrides
from i3x_server.model.builder import ModelBuilder
from i3x_server.prompts.registry import PromptRegistry
from i3x_server.schemas.state import BuildResult
from i3x_server.version import get_server_version

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Bound the request-validation diagnostics embedded in `responseDetail.detail` so a
# pathological payload cannot blow up the error envelope.
_MAX_VALIDATION_DIAGNOSTICS = 10


def _is_tag_name_boundary(value: str, index: int) -> bool:
    if index >= len(value):
        return True
    return not (value[index].isalnum() or value[index] in {"_", "-", ":"})


def _extract_inline_script_bodies(html: str) -> list[str]:
    lower_html = html.lower()
    cursor = 0
    script_bodies: list[str] = []

    while True:
        start = lower_html.find("<script", cursor)
        if start == -1:
            break
        if not _is_tag_name_boundary(lower_html, start + len("<script")):
            cursor = start + 1
            continue

        open_end = html.find(">", start)
        if open_end == -1:
            break

        close_start = lower_html.find("</script", open_end + 1)
        if close_start == -1:
            break
        if not _is_tag_name_boundary(lower_html, close_start + len("</script")):
            cursor = close_start + 1
            continue

        close_end = html.find(">", close_start)
        if close_end == -1:
            break

        script_bodies.append(html[open_end + 1 : close_start])
        cursor = close_end + 1

    return script_bodies


def _status_title(status_code: int) -> str:
    try:
        return http.HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


def _runtime_settings() -> Settings:
    # Same source as every other toggle read (config.settings.get_settings): explicit environment
    # variables still win over the .env file, and launching with plain uvicorn no longer silently
    # ignores the .env toggles that README and .env-example document.
    return get_settings()


def _frontend_inline_script_hashes(frontend_dist: Path) -> tuple[str, ...]:
    if not frontend_dist.exists():
        return ()
    hashes: set[str] = set()
    for html_file in frontend_dist.rglob("*.html"):
        try:
            html = html_file.read_text(encoding="utf-8")
        except OSError:
            continue
        for script_body in _extract_inline_script_bodies(html):
            if not script_body.strip():
                continue
            digest = hashlib.sha256(script_body.encode("utf-8")).digest()
            encoded = base64.b64encode(digest).decode("ascii")
            hashes.add(f"'sha256-{encoded}'")
    return tuple(sorted(hashes))


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
    # Keep the connection supervisor's reconnect lifecycle messages visible.
    logging.getLogger("asyncua.client.client").setLevel(max(level, logging.INFO))


def _prepare_otel_environment() -> None:
    if not settings.otel_enabled:
        return
    os.environ.setdefault("OTEL_SERVICE_NAME", settings.otel_service_name)
    if settings.otel_otlp_endpoint:
        os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", settings.otel_otlp_endpoint)


async def _warm_object_type_context(app: FastAPI, model: BuildResult) -> None:
    started = perf_counter()
    try:
        context = await warm_object_type_context(app, model, app.state.opcua_client)
    except Exception:
        logger.exception("Object type context warm-up failed; will build on first request")
        return
    logger.info(
        "Object type context warmed items=%d duration_s=%.3f",
        len(context.items),
        perf_counter() - started,
    )


async def _run_model_preload(app: FastAPI) -> None:
    try:
        started = asyncio.get_running_loop().time()
        app.state.opcua_client.reset_runtime_metrics()
        build_started = asyncio.get_running_loop().time()
        # Background refresh work runs under the dedicated refresh budget so a rebuild
        # cannot consume the read budget that request handlers share.
        async with model_refresh_budget(app.state.opcua_client):
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
        async with model_refresh_budget(app.state.opcua_client):
            if settings.model_preload_blocking:
                await _warm_object_type_context(app, preload)
            else:
                # Keep background preload from holding model-only requests behind type warm-up.
                # The task copies the refresh-budget context, so warm-up stays throttled.
                app.state.object_type_warm_task = asyncio.create_task(_warm_object_type_context(app, preload))
    except Exception:
        logger.exception("Model preload failed")
        if settings.fail_startup_on_model_preload_error and settings.model_preload_blocking:
            raise
        logger.warning("Continuing without preloaded model; model will build lazily on demand")


async def _run_periodic_model_refresh(app: FastAPI) -> None:
    interval_seconds = settings.model_refresh_interval_seconds
    if interval_seconds <= 0:
        logger.info("Periodic model refresh disabled interval_s=%d", interval_seconds)
        return

    logger.info("Periodic model refresh enabled interval_s=%d", interval_seconds)
    while True:
        await asyncio.sleep(interval_seconds)
        preload_task = cast(asyncio.Task[None] | None, getattr(app.state, "model_preload_task", None))
        if preload_task is not None and not preload_task.done():
            logger.info("Periodic model refresh waiting for preload completion")
            try:
                await preload_task
            except Exception:
                logger.exception("Periodic model refresh observed preload failure; continuing")

        started = perf_counter()
        try:
            # Build outside the lock so readers keep serving the previous model revision
            # while the refresh round-trips run; only the swap is serialized. The refresh
            # budget keeps the rebuild's round-trips from consuming the read budget that
            # request handlers share.
            async with model_refresh_budget(getattr(app.state, "opcua_client", None)):
                built = await app.state.model_builder.build()
            async with app.state.model_lock:
                app.state.model_cache = built
                # Keep the existing object-type context unless the refreshed model is
                # materially different. This avoids a pointless rebuild on equivalent
                # refreshes while still allowing _get_object_type_context() to detect
                # genuine changes via content-derived tokens.
            logger.info(
                "Periodic model refresh finished nodes=%d roots=%d properties=%d actions=%d duration_s=%.3f",
                len(built.nodes_by_id),
                len(built.root_ids),
                len(built.property_to_node),
                len(built.action_to_method),
                perf_counter() - started,
            )
            async with model_refresh_budget(getattr(app.state, "opcua_client", None)):
                await _warm_object_type_context(app, built)
        except Exception:
            logger.exception("Periodic model refresh failed")


def _empty_model_result() -> BuildResult:
    """Empty model placeholder for ``skip_opcua_connect`` and degraded startup."""
    return BuildResult(
        nodes_by_id={},
        root_ids=[],
        children_by_id={},
        instances_by_type_id={},
        property_to_node={},
        action_to_method={},
    )


async def _retry_opcua_connect(app: FastAPI, opcua_client: OpcUaClient) -> None:
    """Startup supervisor for a failed first connect (degraded mode).

    asyncua's reconnect supervisor only exists *after* a successful connect, so a startup
    failure would otherwise leave the process serving forever without an upstream. This loop
    retries with exponential backoff bounded by ``opcua_reconnect_max_delay_seconds``, then
    builds the model and hands periodic refresh back to ``_run_periodic_model_refresh``.
    """
    runtime = _runtime_settings()
    attempt = 0
    while True:
        delay = min(
            runtime.opcua_startup_retry_interval_seconds * 2**attempt,
            runtime.opcua_reconnect_max_delay_seconds,
        )
        await asyncio.sleep(delay)
        attempt += 1
        try:
            await opcua_client.connect()
        except Exception as exc:
            logger.warning(
                "OPC UA startup retry failed endpoint=%s attempt=%d next_delay_s=%.1f error=%s",
                runtime.opcua_endpoint,
                attempt,
                delay,
                exc,
            )
            continue
        app.state.ua_startup_mode = "connected"
        logger.info("OPC UA startup retry connected endpoint=%s attempt=%d", runtime.opcua_endpoint, attempt)
        if runtime.model_preload_on_startup:
            try:
                await _run_model_preload(app)
            except Exception:
                logger.exception("Model preload after startup retry failed; model builds lazily on demand")
        if runtime.model_refresh_interval_seconds > 0 and getattr(app.state, "model_refresh_task", None) is None:
            app.state.model_refresh_task = asyncio.create_task(_run_periodic_model_refresh(app))
        return


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _configure_logging()
    if settings.otel_enabled:
        init_mcp_metrics()
    runtime_toggle_settings = _runtime_settings()
    mcp_enabled = runtime_toggle_settings.enable_mcp
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
        read_concurrency=settings.opcua_read_concurrency,
        refresh_concurrency=settings.opcua_refresh_concurrency,
        browse_roots=settings.opcua_browse_roots,
        metadata_cache_ttl_seconds=settings.opcua_metadata_cache_ttl_seconds,
        connection_monitor_interval_seconds=settings.opcua_connection_monitor_interval_seconds,
        reconnect_request_timeout_seconds=settings.opcua_reconnect_request_timeout_seconds,
        reconnect_max_delay_seconds=settings.opcua_reconnect_max_delay_seconds,
    )
    skip_connect = runtime_toggle_settings.skip_opcua_connect
    logger.info(
        "App startup opcua_endpoint=%s skip_connect=%s log_level=%s "
        "browse_concurrency=%d read_concurrency=%d refresh_concurrency=%d "
        "metadata_cache_ttl_seconds=%d connection_monitor_interval_seconds=%d "
        "reconnect_request_timeout_seconds=%.1f reconnect_max_delay_seconds=%.1f "
        "auth_configured=%s security_mode=%s mcp_enabled=%s",
        settings.opcua_endpoint,
        skip_connect,
        settings.log_level,
        settings.opcua_browse_concurrency,
        settings.opcua_read_concurrency,
        settings.opcua_refresh_concurrency,
        settings.opcua_metadata_cache_ttl_seconds,
        settings.opcua_connection_monitor_interval_seconds,
        settings.opcua_reconnect_request_timeout_seconds,
        settings.opcua_reconnect_max_delay_seconds,
        bool(settings.opcua_username and settings.opcua_password),
        settings.opcua_security_mode,
        mcp_enabled,
    )
    startup_mode = "skipped"
    if not skip_connect:
        startup_mode = "connected"
        try:
            await opcua_client.connect()
        except Exception as exc:
            if not runtime_toggle_settings.allow_degraded_startup:
                raise
            startup_mode = "degraded"
            logger.warning(
                "OPC UA connect failed at startup endpoint=%s error=%s; serving in degraded mode "
                "(empty model) with a background retry supervisor (I3X_ALLOW_DEGRADED_STARTUP=1)",
                settings.opcua_endpoint,
                exc,
            )
    app.state.ua_startup_mode = startup_mode
    app.state.opcua_client = opcua_client
    app.state.model_builder = ModelBuilder(opcua_client)
    app.state.mcp_model_provider = get_or_build_model
    app.state.object_type_context_cache = None
    app.state.object_type_lock = asyncio.Lock()
    app.state.namespace_query_cache = NamespaceSnapshotCache()
    app.state.subscription_service = SubscriptionService(
        opcua_client=opcua_client,
        interval_seconds=settings.subscription_interval_seconds,
        max_updates_per_subscription=settings.subscription_max_updates,
        ttl_seconds=settings.subscription_ttl_seconds,
        seed_initial_values=settings.subscriptions_initial_values,
        native_timeout_refresh_mode=settings.subscription_native_timeout_refresh_mode,
        native_timeout_refresh_keepalives=settings.subscription_native_timeout_refresh_keepalives,
        native_timeout_refresh_max_seconds=settings.subscription_native_timeout_refresh_max_seconds,
        max_concurrent_native_admissions=settings.subscription_max_concurrent_native_admissions,
        native_backoff_seconds=settings.subscription_native_backoff_seconds,
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
        shutdown_mcp_transport(app)

    def _make_chained(sig: int) -> None:
        _old = signal.getsignal(sig)

        def _handler(signum: int, frame: types.FrameType | None) -> None:
            _loop.call_soon_threadsafe(_early_shutdown)
            if callable(_old):
                _old(signum, frame)

        signal.signal(sig, _handler)
        _signal_original_handlers[sig] = _old

    _handled_signals = [signal.SIGINT]
    if hasattr(signal, "SIGTERM"):
        _handled_signals.append(signal.SIGTERM)
    _async_signal_signums: set[int] = set()
    _signal_original_handlers: dict[int, Any] = {}
    for _sig in _handled_signals:
        try:
            # On Unix, prefer asyncio's loop handler so we stay on the event loop thread.
            _loop.add_signal_handler(_sig, _early_shutdown)
            _async_signal_signums.add(_sig)
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
    app.state.model_build_task = None
    app.state.model_preload_task = None
    app.state.model_refresh_task = None
    app.state.object_type_warm_task = None
    app.state.object_type_refresh_task = None
    if mcp_enabled:
        openapi_spec = app.openapi()
        if not isinstance(openapi_spec, dict):
            raise ValueError(f"OpenAPI spec must be dict, got {type(openapi_spec)}")

        app.state.mcp_tools = build_mcp_tools(openapi_spec)
        # The overrides file may be rejected by validation (runtime then applies nothing).
        # Record whether it was applied so `i3x://mcp-overrides` cannot be mistaken for the
        # effective catalog (L11).
        app.state.mcp_overrides_applied = bool(load_overrides())
        app.state.mcp_api_prefix = get_api_prefix(openapi_spec)
        app.state.mcp_transport_sessions = {}
        prompt_overrides = load_prompt_overrides()
        app.state.mcp_prompts = PromptRegistry.load_from_overrides(
            prompt_overrides,
        )
        logger.info(
            "MCP prompt registry loaded from overrides count=%d",
            len(app.state.mcp_prompts.list_metadata()),
        )
    if skip_connect or startup_mode == "degraded":
        app.state.model_cache = _empty_model_result()
        if startup_mode == "degraded":
            app.state.opcua_retry_task = asyncio.create_task(_retry_opcua_connect(app, opcua_client))
    else:
        app.state.model_cache = None
        if settings.model_preload_on_startup:
            if settings.model_preload_blocking:
                logger.info("Model preload at startup enabled (blocking)")
                await _run_model_preload(app)
            else:
                logger.info("Model preload at startup enabled (background)")
                app.state.model_preload_task = asyncio.create_task(_run_model_preload(app))
        app.state.model_refresh_task = asyncio.create_task(_run_periodic_model_refresh(app))
    try:
        yield
    finally:
        logger.info("App shutdown started")
        # Deregister the termination signal handlers installed above. Without this, a
        # later shutdown in the same process (tests, repeated `create_app()`) fires
        # `_early_shutdown` on a closed event loop (`RuntimeError: Event loop is
        # closed`) and chained handlers accumulate onto the previous app's handler.
        for _sig in _handled_signals:
            if _sig in _async_signal_signums:
                with suppress(Exception):
                    _loop.remove_signal_handler(_sig)
            original_handler = _signal_original_handlers.get(_sig)
            if original_handler is not None:
                with suppress(Exception):
                    signal.signal(_sig, original_handler)
        refresh_task = getattr(app.state, "model_refresh_task", None)
        if refresh_task is not None and not refresh_task.done():
            refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await refresh_task
        preload_task = getattr(app.state, "model_preload_task", None)
        if preload_task is not None and not preload_task.done():
            preload_task.cancel()
            with suppress(asyncio.CancelledError):
                await preload_task
        retry_task = getattr(app.state, "opcua_retry_task", None)
        if retry_task is not None and not retry_task.done():
            retry_task.cancel()
            with suppress(asyncio.CancelledError):
                await retry_task
        for task_name in ("object_type_warm_task", "object_type_refresh_task"):
            warm_task = getattr(app.state, task_name, None)
            if warm_task is not None and not warm_task.done():
                warm_task.cancel()
                with suppress(asyncio.CancelledError):
                    await warm_task
        await app.state.subscription_service.close()
        # Close every legacy MCP SSE stream and drop the transport session map. The signal handler
        # only covers terminated processes; a plain shutdown (uvicorn exit, TestClient context)
        # must not leave sessions - and their queues - alive.
        shutdown_mcp_transport(app)
        # Degraded startup may never have established a session; disconnecting an unconnected
        # client would raise during shutdown and mask the real startup failure.
        if app.state.ua_startup_mode == "degraded":
            logger.info("OPC UA disconnect skipped endpoint=%s (never connected)", settings.opcua_endpoint)
        elif app.state.ua_startup_mode != "skipped":
            await opcua_client.disconnect()
        logger.info("App shutdown finished")


def create_app() -> FastAPI:
    mcp_enabled = _runtime_settings().enable_mcp
    _prepare_otel_environment()
    description = (
        "Turn any OPC UA server into an i3X-compliant REST and MCP Enabled API with OpenAPI docs, "
        "JSON, and live SSE streams. No OPC UA expertise required.\n\n"
        "**Workflow:** discover namespaces, types, and objects; use returned element IDs to read or write values. "
        "For changes, create a subscription, register objects, then sync or stream updates, and delete it when done. "
        "Example IDs are placeholders and must be replaced with IDs returned by this server.\n\n"
        "**Errors:** request validation returns HTTP 400, not 422. HTTP errors use the i3X JSON envelope with "
        "`success=false`, `error`, and `responseDetail`. Bulk operations can return HTTP 200 with failed items; "
        "inspect every item's `success` and `responseDetail` rather than relying only on the HTTP status.\n\n"
        "**Production deployment:** terminate HTTPS and enforce authentication/authorization at the configured "
        "reverse proxy. `/v1/info` must remain available without authentication. No HTTP authentication scheme "
        "is enforced by this application itself, so no deployment-specific security scheme is advertised here. "
        "A subscription `clientId` scopes subscriptions; it does not authenticate a caller.\n\n"
        "**Capabilities:** writes require `I3X_ENABLE_WRITES` and upstream permissions. Historical writes also "
        "require upstream HistoryUpdate support. Diagnostics and MCP are gateway extensions, not i3X core routes."
    )
    app = DocumentedFastAPI(
        title="The i3X API Gateway for OPC UA",
        version=get_server_version(),
        description=description,
        contact={"name": "i3x2ua project", "url": "https://github.com/AndreasHeine/i3x2ua"},
        license_info={
            "name": "AGPL-3.0 or commercial license",
            "url": "https://github.com/AndreasHeine/i3x2ua/blob/master/LICENSE",
        },
        openapi_external_docs={
            "description": "Deployment, usage, and OPC UA mapping documentation",
            "url": "https://github.com/AndreasHeine/i3x2ua#readme",
        },
        openapi_tags=[
            {"name": "Info", "description": "Retrieve general information about the server and its capabilities."},
            {"name": "Exploratory", "description": "Browse and discover the address space."},
            {"name": "Query", "description": "Read current or historical values of Objects."},
            {"name": "Update", "description": "Write current or historical data to Objects."},
            {"name": "Subscribe", "description": "Subscribe to data changes for Objects."},
            {"name": "Diagnostics", "description": "Inspect model metrics and OPC UA connectivity."},
            {
                "name": "MCP Protocol",
                "description": ("The JSON-RPC endpoint for MCP requests. MCP clients send requests to POST /mcp."),
            },
            {
                "name": "MCP Helpers",
                "description": (
                    "Local REST-style convenience endpoints for inspecting and invoking MCP tools, prompts, "
                    "resources, and model roots. These are not MCP protocol routes; MCP clients should use POST /mcp."
                ),
            },
        ],
        lifespan=lifespan,
        generate_unique_id_function=_readable_operation_id,
        telemetry={
            "tracing": settings.otel_enabled,
            "metrics": settings.otel_enabled,
            "logs": False,
            "operation_spans": settings.otel_enabled,
            "auto_configure": settings.otel_enabled,
        },
    )
    # Added first so it is the innermost middleware and sees uncompressed responses.
    app.add_middleware(ModelRevisionMiddleware)
    # Spec CORE-04 / Implementation Guide § Transport & Encoding: any response to a request
    # with Accept-Encoding: gzip must carry Content-Encoding: gzip, so compression must apply
    # even to small bodies (minimum_size=1).
    app.add_middleware(GZipMiddleware, minimum_size=1)
    cors_origins = settings.cors_allowed_origins
    app.state.mcp_allowed_origins = [origin for origin in cors_origins if origin != "*"]
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "Accept", "Authorization", "If-None-Match", "MCP-Protocol-Version"],
            expose_headers=["ETag", MODEL_REVISION_HEADER],
            allow_credentials=False,
        )

    project_root = PROJECT_ROOT
    frontend_dist = project_root / "dist"
    frontend_script_hashes = _frontend_inline_script_hashes(frontend_dist)
    static_dir = project_root / "static"
    # Backward-compatible fallback for local setups that still use the legacy img folder.
    if not static_dir.exists():
        static_dir = project_root / "img"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.exception_handler(NotModifiedError)
    async def handle_not_modified(request: Request, exc: NotModifiedError) -> Response:
        del request, exc
        # ModelRevisionMiddleware adds the ETag and Cache-Control headers.
        return Response(status_code=304)

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        del request
        # Keep the field diagnostics (loc/msg) in `responseDetail.detail`: for a bulk
        # API the message alone is the difference between a fast fix and a guessing loop.
        message = "Invalid request payload or parameters"
        errors = list(exc.errors())
        diagnostics: list[str] = []
        for item in errors[:_MAX_VALIDATION_DIAGNOSTICS]:
            loc = ".".join(str(token) for token in item.get("loc", ()))
            msg = str(item.get("msg", "invalid value"))
            diagnostics.append(f"{loc}: {msg}" if loc else msg)
        detail = message
        if diagnostics:
            suffix = "; ".join(diagnostics)
            if len(errors) > len(diagnostics):
                suffix += f"; (+{len(errors) - len(diagnostics)} more)"
            detail = f"{message} ({suffix})"
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
                    "detail": detail,
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
        # `ApplicationServiceError.details` (e.g. {"cause": ...}) must reach the client;
        # it used to be stored but never rendered.
        detail = exc.message
        diagnostics = format_error_details(exc.details)
        if diagnostics is not None:
            detail = f"{exc.message} ({diagnostics})"
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "success": False,
                "error": {"code": int(exc.status_code), "message": exc.message},
                "responseDetail": {
                    "title": _status_title(int(exc.status_code)),
                    "status": int(exc.status_code),
                    "detail": detail,
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
                f"script-src 'self' {' '.join(frontend_script_hashes)}; connect-src 'self'; object-src 'none'; "
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

    error_responses: dict[int | str, dict[str, Any]] = {
        400: {
            "model": HttpErrorResponse,
            "description": "Invalid request, including body or parameter validation errors.",
        },
        500: {"model": HttpErrorResponse, "description": "Internal server error."},
    }
    app.include_router(v1_router, responses=error_responses)
    app.include_router(ua_router, responses=error_responses)
    # Container readiness probe; excluded from the OpenAPI schema (docs/PRODUCTION_HTTPS_GUIDE.md).
    app.include_router(health_router)
    if mcp_enabled:
        app.include_router(mcp_router, responses=error_responses)

    if frontend_dist.exists():
        app.frontend("/", directory=str(frontend_dist), fallback=None)

        # Prerendered pages carry relative asset URLs ("./_app/..."), and the boot script
        # derives the asset base from the URL. `app.frontend()` serves dist/<path>/index.html
        # for "/path" without a canonical redirect, so a directly entered or shared URL like
        # "/view?endpoint=..." renders HTML whose modules resolve to "/view/_app/..." and 404:
        # the page stays an inert "Loading..." skeleton. Answer GET/HEAD requests for paths
        # whose directory contains an index.html with a redirect to the trailing-slash URL
        # (query preserved), where relative assets resolve correctly. The page set is walked
        # once at startup so per-request handling is a path lookup, not filesystem stats.
        frontend_dist_root = frontend_dist.resolve()
        frontend_page_paths: set[str] = set()
        for dirpath, _dirnames, filenames in os.walk(frontend_dist_root):
            if "index.html" not in filenames:
                continue
            relative = os.path.relpath(dirpath, frontend_dist_root)
            if relative == ".":
                continue
            frontend_page_paths.add("/" + relative.replace("\\", "/"))

        @app.middleware("http")
        async def redirect_frontend_pages_to_canonical_url(
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
        ) -> Response:
            if request.method not in {"GET", "HEAD"} or request.url.path.endswith("/"):
                return await call_next(request)
            normalized = "/" + posixpath.normpath(request.url.path.lstrip("/"))
            if normalized in frontend_page_paths:
                redirect_url = request.url.path + "/"
                if request.url.query:
                    redirect_url += f"?{request.url.query}"
                return RedirectResponse(url=redirect_url)
            return await call_next(request)
    else:
        logger.warning("Frontend dist directory not found at %s; UI pages are unavailable", frontend_dist)

    return app
