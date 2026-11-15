"""Readiness must reflect what the gateway can actually serve.

``/health`` is deliberately static so a reconnecting upstream never flips container liveness.
That also means a gateway with no model and no upstream connection stays "healthy" and keeps
receiving traffic (``I3X_ALLOW_DEGRADED_STARTUP``, ``I3X_MODEL_PRELOAD_BLOCKING=false``). The
``i3x_model_built`` / ``i3x_upstream_connected`` gauges describe that state but nothing probes it,
so ``/ready`` mirrors them: ready only when a model exists and the connection is Connected.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from i3x_server.api.health import router as health_router
from i3x_server.application.state import gateway_state
from i3x_server.infrastructure.opcua.client import OpcUaConnectionSnapshot

_SNAPSHOT_SINCE = datetime(2026, 1, 1, 8, 11, 24, tzinfo=timezone.utc)


# The typed container is mutated from tests exactly like the runtime assigns it, so the state
# reads go through an ``Any`` view of ``app.state`` (same pattern as the other route tests).
def _state(client: TestClient) -> Any:
    return cast(Any, gateway_state(client.app))


def _metric_value(body: str, sample: str) -> float | None:
    """Value of a Prometheus sample line, or ``None`` when the line is absent."""
    for line in body.splitlines():
        if line.startswith(f"{sample} "):
            return float(line.rsplit(" ", 1)[1])
    return None


def _snapshot(state: str) -> OpcUaConnectionSnapshot:
    return OpcUaConnectionSnapshot(state=state, endpoint="opc.tcp://local:4843", since=_SNAPSHOT_SINCE)


def _set_upstream_state(client: TestClient, state: str) -> None:
    cast(Any, _state(client).opcua_client)._connection_snapshot = _snapshot(state)


def test_ready_reports_ready_when_a_model_exists_and_the_upstream_is_connected(client: TestClient) -> None:
    """The fixture app has a built model and a Connected fake upstream."""
    response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["model_built"] is True
    assert body["upstream_connected"] is True
    assert body["reasons"] == []


def test_ready_is_not_ready_without_a_model(client: TestClient) -> None:
    """A gateway that has not built the address-space model cannot answer i3X queries."""
    _state(client).model_cache = None

    response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["model_built"] is False
    assert body["upstream_connected"] is True
    assert body["reasons"] == ["model_not_built"]


@pytest.mark.parametrize("state", ["Disconnected", "Reconnecting"])
def test_ready_is_not_ready_while_the_upstream_is_not_connected(client: TestClient, state: str) -> None:
    """Only Connected counts: a reconnecting gateway answers from a stale model."""
    _set_upstream_state(client, state)

    response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["upstream_connected"] is False
    assert body["model_built"] is True
    assert body["reasons"] == ["upstream_not_connected"]


def test_ready_is_not_ready_without_an_upstream_client(client: TestClient) -> None:
    """Skip-connect and degraded startup have no connection at all."""
    _state(client).opcua_client = None

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["reasons"] == ["upstream_not_connected"]


def test_ready_reports_every_unavailable_condition(client: TestClient) -> None:
    """Reasons are stable tokens and stay ordered model first, then upstream."""
    _state(client).model_cache = None
    _set_upstream_state(client, "Disconnected")

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["reasons"] == ["model_not_built", "upstream_not_connected"]


def test_ready_survives_an_unreadable_upstream_state(client: TestClient) -> None:
    """A snapshot read that raises must degrade the answer, not fail the probe."""

    class _Unreadable:
        def get_connection_snapshot(self) -> OpcUaConnectionSnapshot:
            raise RuntimeError("adapter state unavailable")

    _state(client).opcua_client = _Unreadable()

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["reasons"] == ["upstream_state_unavailable"]


def test_ready_survives_a_client_without_a_snapshot_reader(client: TestClient) -> None:
    """The port exposes the snapshot reader optionally, like the other diagnostic routes."""
    _state(client).opcua_client = SimpleNamespace()

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["reasons"] == ["upstream_state_unavailable"]


def test_ready_reads_only_local_state(client: TestClient) -> None:
    """Probes run every few seconds, so readiness must not touch the upstream."""
    calls: list[str] = []

    def _snapshot_reader() -> OpcUaConnectionSnapshot:
        calls.append("snapshot")
        return _snapshot("Connected")

    _state(client).opcua_client = SimpleNamespace(get_connection_snapshot=_snapshot_reader)

    assert client.get("/ready").status_code == 200
    assert calls == ["snapshot"]


def test_liveness_stays_static_while_readiness_is_not_ready(client: TestClient) -> None:
    """Regression: a degraded instance must not be killed by a liveness probe."""
    _state(client).model_cache = None
    _set_upstream_state(client, "Reconnecting")

    assert client.get("/ready").status_code == 503
    assert client.get("/health").status_code == 200


def test_ready_is_excluded_from_the_openapi_document(client: TestClient) -> None:
    """Gateway observability lives outside the i3X contract, like /health and /metrics."""
    paths = client.get("/openapi.json").json()["paths"]

    assert "/ready" not in paths
    assert "/health" not in paths


def test_ready_routes_share_the_diagnostics_tag_with_liveness() -> None:
    """Both probes are one surface: same router, same tag, root paths."""
    routes = {route.path: route for route in health_router.routes if isinstance(route, APIRoute)}

    assert set(routes) == {"/health", "/ready"}
    assert routes["/ready"].tags == ["Diagnostics"]
    assert routes["/ready"].response_model is not None


def test_ready_agrees_with_the_prometheus_gauges(client: TestClient) -> None:
    """The probe and the gauges describe the same two signals, so alerts and probes agree."""
    metrics = client.get("/metrics").text
    assert _metric_value(metrics, "i3x_model_built") == 1.0
    assert _metric_value(metrics, "i3x_upstream_connected") == 1.0
    assert client.get("/ready").status_code == 200

    _state(client).model_cache = None
    _set_upstream_state(client, "Reconnecting")
    metrics = client.get("/metrics").text
    assert _metric_value(metrics, "i3x_model_built") == 0.0
    assert _metric_value(metrics, "i3x_upstream_connected") == 0.0
    assert client.get("/ready").status_code == 503
