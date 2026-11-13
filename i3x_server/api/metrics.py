"""Prometheus scrape endpoint for the gateway's own diagnostics.

The i3X contract defines no metrics surface, so ``/metrics`` lives outside ``/v1`` and is
excluded from the OpenAPI document - it never enters the MCP tool catalog. The route mirrors the
JSON diagnostics the gateway already reports (``/ua/metrics``, ``/ua/connection``, the cached
model) into gauges and adds the request instruments recorded by the request middleware
(:mod:`i3x_server.http_metrics`).

A diagnostics read that fails is reported as ``0`` instead of failing the scrape: a Prometheus
poll must not error out because the upstream is reconnecting, which would itself page the
operator for a state the gauges are meant to describe.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest

from i3x_server.application.ports.opcua import OpcUaClientProtocol
from i3x_server.application.ports.subscription import SubscriptionServicePort
from i3x_server.application.state import GatewayState, gateway_state

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Diagnostics"])

# Mirrors of the JSON diagnostics. Gauges, not counters: the values are snapshots taken from the
# runtime at scrape time, and a counter would imply every value was incremented by the gateway.
model_nodes = Gauge("i3x_model_nodes", "Nodes in the cached i3X model (0 when nothing is built)")
subscriptions_active = Gauge("i3x_subscriptions_active", "Subscriptions the gateway currently tracks")
upstream_connected = Gauge("i3x_upstream_connected", "1 when the upstream OPC UA connection is Connected")
upstream_requests = Gauge(
    "i3x_opcua_requests",
    "Upstream OPC UA request counts, mirroring GET /ua/metrics",
    ["operation"],
)
# Request metrics and model state are separate signals: a gateway that serves requests with no
# model is exactly the situation an operator needs an alert for.
model_built = Gauge("i3x_model_built", "1 when the address-space model is built")

_UPSTREAM_OPERATIONS = (
    ("read", "read_count"),
    ("write", "write_count"),
    ("browse", "browse_count"),
    ("method", "method_call_count"),
    ("history_read", "history_read_count"),
    ("history_write", "history_write_count"),
    ("failed", "failed_request_count"),
)


def _observe_upstream(client: OpcUaClientProtocol) -> None:
    """Mirror the connection state and upstream request counters from the adapter.

    A snapshot that cannot be read makes both signals untrustworthy, so the connection gauge and
    every operation gauge fall back to ``0`` rather than keeping a value from an earlier scrape.
    """
    try:
        snapshot = client.get_connection_snapshot()
        connected = snapshot.state == "Connected"
    except Exception:
        logger.warning("Upstream connection state unavailable for /metrics; reporting 0", exc_info=True)
        connected = False
    upstream_connected.set(1 if connected else 0)

    try:
        metrics = client.snapshot_request_metrics()
    except Exception:
        logger.warning("Upstream request metrics unavailable for /metrics; reporting 0", exc_info=True)
        for operation, _attr in _UPSTREAM_OPERATIONS:
            upstream_requests.labels(operation=operation).set(0)
        return
    for operation, attr in _UPSTREAM_OPERATIONS:
        upstream_requests.labels(operation=operation).set(getattr(metrics, attr, 0))


async def _observe_subscriptions(service: SubscriptionServicePort | None) -> None:
    """Mirror the number of tracked subscriptions, or ``0`` when the runtime has none/failed."""
    if service is None:
        subscriptions_active.set(0)
        return
    try:
        subscriptions_active.set(len(await service.list_subscriptions(None)))
    except Exception:
        logger.warning("Subscription inventory unavailable for /metrics; reporting 0", exc_info=True)
        subscriptions_active.set(0)


def _observe_model(state: GatewayState) -> None:
    """Mirror the cached model size and whether a model exists at all."""
    model = state.model_cache
    if model is None:
        model_nodes.set(0)
        model_built.set(0)
        return
    model_nodes.set(len(model.nodes_by_id))
    model_built.set(1)


@router.get("/metrics", include_in_schema=False, response_class=Response)
async def scrape_metrics(request: Request) -> Response:
    """Scrape endpoint in the Prometheus text format.

    Unauthenticated like every other endpoint of this gateway, so deployments that only want
    scrapers to reach it must restrict ``/metrics`` at the reverse proxy.
    """
    state = gateway_state(request)
    _observe_model(state)
    await _observe_subscriptions(state.subscription_service)
    if state.opcua_client is None:
        upstream_connected.set(0)
        for operation, _attr in _UPSTREAM_OPERATIONS:
            upstream_requests.labels(operation=operation).set(0)
    else:
        _observe_upstream(state.opcua_client)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
