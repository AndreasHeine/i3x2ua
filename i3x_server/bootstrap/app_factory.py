"""Composition root: builds the FastAPI app and owns its lifespan.

Everything with behaviour lives in sibling modules of this package, so this file is only the
assembly order:

- :mod:`i3x_server.bootstrap.openapi_config` - OpenAPI document metadata and operation ids,
- :mod:`i3x_server.bootstrap.error_handlers` - the i3X error envelope for every failure type,
- :mod:`i3x_server.bootstrap.request_middlewares` - transport, security headers, request observability,
- :mod:`i3x_server.bootstrap.frontend` - static assets, CSP script hashes, canonical page URLs,
- :mod:`i3x_server.bootstrap.runtime_setup` - OPC UA client, subscriptions, MCP state, model
  tasks, signal handlers, and shutdown.

``create_app`` and ``lifespan`` stay here because they are the entry points tests patch and the
only place that decides in which order the layers are assembled.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from i3x_server.api.health import router as health_router
from i3x_server.api.mcp import router as mcp_router
from i3x_server.api.metrics import router as metrics_router
from i3x_server.api.openapi import DocumentedFastAPI
from i3x_server.api.ua import router as ua_router
from i3x_server.api.v1 import router as v1_router
from i3x_server.application.dependencies import get_or_build_model
from i3x_server.application.state import GatewayState, gateway_state, install_gateway_state
from i3x_server.bootstrap import error_handlers, frontend, openapi_config, request_middlewares, runtime_setup
from i3x_server.config.settings import get_settings
from i3x_server.model.builder import ModelBuilder
from i3x_server.version import get_server_version


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the runtime, serve, then close it in reverse startup order."""
    runtime_setup._configure_logging()
    runtime_setup.configure_otel_metrics()
    runtime = get_settings()
    mcp_enabled = runtime.enable_mcp

    opcua_client = runtime_setup.build_opcua_client()
    startup_mode = await runtime_setup.connect_upstream(opcua_client, runtime)

    state = gateway_state(app)
    state.ua_startup_mode = startup_mode
    state.opcua_client = opcua_client
    state.model_builder = ModelBuilder(opcua_client)
    state.mcp_model_provider = get_or_build_model
    subscription_service = runtime_setup.build_subscription_service(opcua_client)
    state.subscription_service = subscription_service

    # Installed before the model tasks so an early termination signal still closes live
    # streams, and before MCP wiring so the transport map it clears is the one this startup owns.
    signal_handlers = runtime_setup.install_shutdown_signal_handlers(app, subscription_service)
    if mcp_enabled:
        runtime_setup.configure_mcp_state(app, state)
    await runtime_setup.start_model_tasks(app, state, startup_mode, opcua_client)

    try:
        yield
    finally:
        await runtime_setup.shutdown_runtime(app, state, opcua_client, subscription_service, signal_handlers)


def create_app() -> FastAPI:
    """Build the gateway app: document metadata, middleware stack, handlers, and routers."""
    mcp_enabled = get_settings().enable_mcp
    runtime_setup._prepare_otel_environment()
    app = DocumentedFastAPI(
        title="The i3X API Gateway for OPC UA",
        version=get_server_version(),
        description=openapi_config.APP_DESCRIPTION,
        contact=openapi_config.APP_CONTACT,
        license_info=openapi_config.APP_LICENSE_INFO,
        openapi_external_docs=openapi_config.OPENAPI_EXTERNAL_DOCS,
        openapi_tags=openapi_config.OPENAPI_TAGS,
        lifespan=lifespan,
        generate_unique_id_function=openapi_config._readable_operation_id,
        telemetry=openapi_config.telemetry_config(),
    )
    # The typed state container is installed here, before the lifecycle runs, so every
    # collaborator field exists from the first request onward. Fields the lifespan fills
    # later keep their declared defaults.
    install_gateway_state(app, GatewayState())
    request_middlewares.add_transport_middlewares(app)

    # The CSP for UI pages enumerates the inline scripts of the prerendered bundle, so the
    # hashes are derived from `dist/` before the security-header middleware is added.
    frontend_dist = frontend.frontend_dist_directory()
    request_middlewares.add_security_headers(app, frontend.inline_script_hashes(frontend_dist))
    request_middlewares.add_request_observability(app)

    error_handlers.register_exception_handlers(app)
    frontend.mount_static_assets(app)

    app.include_router(v1_router, responses=openapi_config.ERROR_RESPONSES)
    app.include_router(ua_router, responses=openapi_config.ERROR_RESPONSES)
    # Prometheus scrape endpoint; outside /v1 and excluded from the OpenAPI schema, so it is
    # never offered as an MCP tool (docs/PRODUCTION_HTTPS_GUIDE.md).
    app.include_router(metrics_router)
    # Container readiness probe; excluded from the OpenAPI schema (docs/PRODUCTION_HTTPS_GUIDE.md).
    app.include_router(health_router)
    if mcp_enabled:
        app.include_router(mcp_router, responses=openapi_config.ERROR_RESPONSES)

    frontend.install_frontend(app, frontend_dist)
    return app
