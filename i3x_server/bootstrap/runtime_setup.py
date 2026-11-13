"""Runtime wiring for the gateway lifespan.

The lifespan is a sequence of lifecycle steps, each owned here so ``app_factory`` stays a
composition root: build the collaborators, connect the upstream, install the termination signal
handlers, wire MCP state, start the model tasks, and shut everything down in reverse order.

``_run_model_preload`` / ``_run_periodic_model_refresh`` keep their names because they are the
patched seams of the startup tests, and every step reads the settings through the shared
``settings`` snapshot or ``get_settings()`` exactly as the lifecycle did before.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import types
from contextlib import suppress
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from fastapi import FastAPI

from i3x_server.api.mcp.jsonrpc import shutdown_mcp_transport
from i3x_server.api.v1.objecttype_helpers import warm_object_type_context
from i3x_server.application.state import GatewayState, gateway_state
from i3x_server.config.settings import Settings, get_settings, settings
from i3x_server.domain.ports.opcua import OpcUaClientProtocol
from i3x_server.infrastructure.opcua.client import OpcUaClient, model_refresh_budget
from i3x_server.infrastructure.subscriptions.service import SubscriptionService
from i3x_server.mcp.invocation import get_api_prefix, init_mcp_metrics
from i3x_server.mcp.tool_definitions import build_mcp_tools, load_overrides, load_prompt_overrides
from i3x_server.model.builder import ModelBuilder
from i3x_server.prompts.registry import PromptRegistry
from i3x_server.schemas.state import BuildResult

logger = logging.getLogger(__name__)


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
    """Seed the standard OTEL environment variables from the gateway settings.

    ``setdefault`` keeps deployment-supplied values authoritative: an operator setting
    ``OTEL_SERVICE_NAME`` in the environment wins over ``I3X_OTEL_SERVICE_NAME``.
    """
    if not settings.otel_enabled:
        return
    os.environ.setdefault("OTEL_SERVICE_NAME", settings.otel_service_name)
    if settings.otel_otlp_endpoint:
        os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", settings.otel_otlp_endpoint)


def _installed_client(state: GatewayState) -> OpcUaClientProtocol:
    """Lifecycle helpers run after startup installed the client; a missing client is a bug."""
    client = state.opcua_client
    if client is None:
        raise RuntimeError("OPC UA client is not installed on the app state")
    return client


def _installed_model_builder(state: GatewayState) -> ModelBuilder:
    """Same guarantee as :func:`_installed_client`, for the model builder."""
    builder = state.model_builder
    if builder is None:
        raise RuntimeError("ModelBuilder is not installed on the app state")
    return builder


def build_opcua_client() -> OpcUaClient:
    """Construct the asyncua adapter from the settings snapshot."""
    return OpcUaClient(
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


def build_subscription_service(opcua_client: OpcUaClientProtocol) -> SubscriptionService:
    """Construct the subscription runtime from the settings snapshot."""
    return SubscriptionService(
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
        max_subscriptions=settings.subscription_max_count,
        subtree_max_nodes=settings.subscription_subtree_max_nodes,
        native_max_bins=settings.subscription_native_max_bins,
        native_max_monitored_items=settings.subscription_native_max_monitored_items,
    )


async def connect_upstream(opcua_client: OpcUaClient, runtime: Settings) -> str:
    """Connect the upstream and report the startup mode the lifespan records.

    ``I3X_ALLOW_DEGRADED_STARTUP=1`` turns an unreachable upstream into a degraded start
    (empty model plus the retry supervisor) instead of aborting startup; by default the failure
    propagates, so a misconfigured deployment never serves an empty model silently.
    """
    skip_connect = runtime.skip_opcua_connect
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
        runtime.enable_mcp,
    )
    if skip_connect:
        return "skipped"
    try:
        await opcua_client.connect()
    except Exception as exc:
        if not runtime.allow_degraded_startup:
            raise
        logger.warning(
            "OPC UA connect failed at startup endpoint=%s error=%s; serving in degraded mode "
            "(empty model) with a background retry supervisor (I3X_ALLOW_DEGRADED_STARTUP=1)",
            settings.opcua_endpoint,
            exc,
        )
        return "degraded"
    return "connected"


async def _warm_object_type_context(app: FastAPI, model: BuildResult) -> None:
    started = perf_counter()
    try:
        context = await warm_object_type_context(app, model, _installed_client(gateway_state(app)))
    except Exception:
        logger.exception("Object type context warm-up failed; will build on first request")
        return
    logger.info(
        "Object type context warmed items=%d duration_s=%.3f",
        len(context.items),
        perf_counter() - started,
    )


async def _run_model_preload(app: FastAPI) -> None:
    state = gateway_state(app)
    try:
        started = asyncio.get_running_loop().time()
        client = _installed_client(state)
        client.reset_runtime_metrics()
        build_started = asyncio.get_running_loop().time()
        # Background refresh work runs under the dedicated refresh budget so a rebuild
        # cannot consume the read budget that request handlers share.
        async with model_refresh_budget(client):
            preload = await _installed_model_builder(state).build()
        build_duration_s = asyncio.get_running_loop().time() - build_started
        state.model_cache = preload
        metrics = client.snapshot_runtime_metrics()
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
        async with model_refresh_budget(client):
            if settings.model_preload_blocking:
                await _warm_object_type_context(app, preload)
            else:
                # Keep background preload from holding model-only requests behind type warm-up.
                # The task copies the refresh-budget context, so warm-up stays throttled.
                state.object_type_warm_task = asyncio.create_task(_warm_object_type_context(app, preload))
    except Exception:
        logger.exception("Model preload failed")
        if settings.fail_startup_on_model_preload_error and settings.model_preload_blocking:
            raise
        logger.warning("Continuing without preloaded model; model will build lazily on demand")


async def _run_periodic_model_refresh(app: FastAPI) -> None:
    state = gateway_state(app)
    interval_seconds = settings.model_refresh_interval_seconds
    if interval_seconds <= 0:
        logger.info("Periodic model refresh disabled interval_s=%d", interval_seconds)
        return

    logger.info("Periodic model refresh enabled interval_s=%d", interval_seconds)
    while True:
        await asyncio.sleep(interval_seconds)
        preload_task = state.model_preload_task
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
            client = _installed_client(state)
            async with model_refresh_budget(client):
                built = await _installed_model_builder(state).build()
            async with state.model_lock:
                state.model_cache = built
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
            async with model_refresh_budget(client):
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
    runtime = get_settings()
    state = gateway_state(app)
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
        state.ua_startup_mode = "connected"
        logger.info("OPC UA startup retry connected endpoint=%s attempt=%d", runtime.opcua_endpoint, attempt)
        if runtime.model_preload_on_startup:
            try:
                await _run_model_preload(app)
            except Exception:
                logger.exception("Model preload after startup retry failed; model builds lazily on demand")
        if runtime.model_refresh_interval_seconds > 0 and state.model_refresh_task is None:
            state.model_refresh_task = asyncio.create_task(_run_periodic_model_refresh(app))
        return


def configure_mcp_state(app: FastAPI, state: GatewayState) -> None:
    """Derive the MCP tool catalog and prompt registry from the OpenAPI document."""
    openapi_spec = app.openapi()
    if not isinstance(openapi_spec, dict):
        raise ValueError(f"OpenAPI spec must be dict, got {type(openapi_spec)}")

    state.mcp_tools = build_mcp_tools(openapi_spec)
    # The overrides file may be rejected by validation (runtime then applies nothing).
    # Record whether it was applied so `i3x://mcp-overrides` cannot be mistaken for the
    # effective catalog (L11).
    state.mcp_overrides_applied = bool(load_overrides())
    state.mcp_api_prefix = get_api_prefix(openapi_spec)
    # A startup owns its transport sessions; a previous lifespan in the same process
    # (tests, repeated create_app) must not carry its queues over.
    state.mcp_transport_sessions = {}
    prompt_overrides = load_prompt_overrides()
    state.mcp_prompts = PromptRegistry.load_from_overrides(
        prompt_overrides,
    )
    logger.info(
        "MCP prompt registry loaded from overrides count=%d",
        len(state.mcp_prompts.list_metadata()),
    )


@dataclass(slots=True)
class ShutdownSignalHandlers:
    """Termination signal handlers installed by :func:`install_shutdown_signal_handlers`."""

    loop: asyncio.AbstractEventLoop
    signals: list[signal.Signals]
    async_signums: set[signal.Signals] = field(default_factory=set)
    originals: dict[int, Any] = field(default_factory=dict)

    def restore(self) -> None:
        """Deregister the handlers the lifespan installed.

        Without this, a later shutdown in the same process (tests, repeated ``create_app()``)
        fires ``_early_shutdown`` on a closed event loop (``RuntimeError: Event loop is
        closed``) and chained handlers accumulate onto the previous app's handler.
        """
        for sig in self.signals:
            if sig in self.async_signums:
                with suppress(Exception):
                    self.loop.remove_signal_handler(sig)
            original_handler = self.originals.get(sig)
            if original_handler is not None:
                with suppress(Exception):
                    signal.signal(sig, original_handler)


def install_shutdown_signal_handlers(app: FastAPI, subscription_service: SubscriptionService) -> ShutdownSignalHandlers:
    """Install a chained termination-signal handler and return it for shutdown.

    Active SSE streaming connections must be closed *before* Uvicorn's "Waiting for connections
    to close" phase. On Windows (and as a fallback on Unix), Uvicorn uses ``signal.signal()``, so
    the existing handler is saved and wrapped; on Unix, asyncio's ``add_signal_handler`` is tried
    first so the callback already runs on the event loop thread.
    """
    loop = asyncio.get_event_loop()

    def _early_shutdown() -> None:
        """Called from the asyncio event loop when a termination signal fires."""
        subscription_service.initiate_shutdown()
        shutdown_mcp_transport(app)

    def _make_chained(handlers: ShutdownSignalHandlers, sig: int) -> None:
        old = signal.getsignal(sig)

        def _handler(signum: int, frame: types.FrameType | None) -> None:
            handlers.loop.call_soon_threadsafe(_early_shutdown)
            if callable(old):
                old(signum, frame)

        signal.signal(sig, _handler)
        handlers.originals[sig] = old

    signals = [signal.SIGINT]
    if hasattr(signal, "SIGTERM"):
        signals.append(signal.SIGTERM)
    handlers = ShutdownSignalHandlers(loop=loop, signals=signals)
    for sig in signals:
        try:
            # On Unix, prefer asyncio's loop handler so we stay on the event loop thread.
            loop.add_signal_handler(sig, _early_shutdown)
            handlers.async_signums.add(sig)
        except (NotImplementedError, OSError, RuntimeError):
            # Windows: asyncio does not support add_signal_handler; chain with signal.signal.
            try:
                _make_chained(handlers, sig)
            except (ValueError, OSError):
                # signal.signal() only works from the main thread.
                # In test environments the lifespan runs in a worker thread, so skip
                # signal registration silently rather than crashing.
                pass
    return handlers


async def start_model_tasks(app: FastAPI, state: GatewayState, startup_mode: str, opcua_client: OpcUaClient) -> None:
    """Seed the model cache and start the background model tasks for the startup mode."""
    if startup_mode == "skipped":
        state.model_cache = _empty_model_result()
        return
    if startup_mode == "degraded":
        state.model_cache = _empty_model_result()
        state.opcua_retry_task = asyncio.create_task(_retry_opcua_connect(app, opcua_client))
        return

    state.model_cache = None
    if settings.model_preload_on_startup:
        if settings.model_preload_blocking:
            logger.info("Model preload at startup enabled (blocking)")
            await _run_model_preload(app)
        else:
            logger.info("Model preload at startup enabled (background)")
            state.model_preload_task = asyncio.create_task(_run_model_preload(app))
    state.model_refresh_task = asyncio.create_task(_run_periodic_model_refresh(app))


async def shutdown_runtime(
    app: FastAPI,
    state: GatewayState,
    opcua_client: OpcUaClient,
    subscription_service: SubscriptionService,
    signal_handlers: ShutdownSignalHandlers,
) -> None:
    """Close the runtime in reverse startup order: signals, tasks, subscriptions, upstream."""
    logger.info("App shutdown started")
    signal_handlers.restore()
    # Background tasks must not outlive the app: cancel and await each one so their
    # cleanup runs before the client disconnect below.
    for task in (
        state.model_refresh_task,
        state.model_preload_task,
        state.opcua_retry_task,
        state.object_type_warm_task,
        state.object_type_refresh_task,
    ):
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    await subscription_service.close()
    # Close every legacy MCP SSE stream and drop the transport session map. The signal handler
    # only covers terminated processes; a plain shutdown (uvicorn exit, TestClient context)
    # must not leave sessions - and their queues - alive.
    shutdown_mcp_transport(app)
    # Degraded startup may never have established a session; disconnecting an unconnected
    # client would raise during shutdown and mask the real startup failure.
    if state.ua_startup_mode == "degraded":
        logger.info("OPC UA disconnect skipped endpoint=%s (never connected)", settings.opcua_endpoint)
    elif state.ua_startup_mode != "skipped":
        await opcua_client.disconnect()
    logger.info("App shutdown finished")


def configure_otel_metrics() -> None:
    """Register the MCP metric instruments once telemetry is enabled."""
    if settings.otel_enabled:
        init_mcp_metrics()
