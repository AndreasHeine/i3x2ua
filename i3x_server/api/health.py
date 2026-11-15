"""Liveness and readiness probes for the gateway process.

``/health`` is the liveness route and stays static: a reconnecting upstream or an unbuilt model
must not flip container health and get a working process killed. ``/ready`` answers the question
the static route cannot - whether this instance can serve i3X traffic right now - by mirroring
the ``i3x_model_built`` and ``i3x_upstream_connected`` gauges, so probes and alerts describe the
same two signals.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from i3x_server.application.state import GatewayState, gateway_state
from i3x_server.domain.ports.opcua import OpcUaClientProtocol

router = APIRouter(tags=["Diagnostics"])

# Only this adapter state counts as ready: Reconnecting and Disconnected answer from a model
# that may be stale, which is exactly what a readiness probe should stop routing to.
_CONNECTED_STATE = "Connected"

# Stable reason tokens rather than prose, so an operator can key on them in a dashboard.
_MODEL_NOT_BUILT = "model_not_built"
_UPSTREAM_NOT_CONNECTED = "upstream_not_connected"
_UPSTREAM_STATE_UNAVAILABLE = "upstream_state_unavailable"


class HealthResponse(BaseModel):
    status: str = "ok"


class ReadinessResponse(BaseModel):
    status: str
    model_built: bool
    upstream_connected: bool
    reasons: list[str] = Field(default_factory=list)


def _upstream_signal(client: OpcUaClientProtocol | None) -> tuple[bool, str | None]:
    """Whether the upstream connection counts as ready, plus the reason token when it does not.

    The snapshot is a local read of the adapter's connection state - probes fire every few
    seconds and must not add an OPC UA round-trip. The port exposes the reader optionally and the
    adapter can fail to answer, so both cases degrade to a reason token instead of raising: a
    probe that errors is indistinguishable from a probe that is unreachable.
    """
    if client is None:
        return False, _UPSTREAM_NOT_CONNECTED
    snapshot_reader = getattr(client, "get_connection_snapshot", None)
    if not callable(snapshot_reader):
        return False, _UPSTREAM_STATE_UNAVAILABLE
    try:
        state = str(snapshot_reader().state)
    except Exception:
        return False, _UPSTREAM_STATE_UNAVAILABLE
    if state == _CONNECTED_STATE:
        return True, None
    return False, _UPSTREAM_NOT_CONNECTED


def _readiness(state: GatewayState) -> ReadinessResponse:
    """Mirror the model and upstream gauges as a probe answer, listing what is missing."""
    reasons: list[str] = []
    model_built = state.model_cache is not None
    if not model_built:
        reasons.append(_MODEL_NOT_BUILT)
    connected, reason = _upstream_signal(state.opcua_client)
    if reason is not None:
        reasons.append(reason)
    return ReadinessResponse(
        status="ready" if not reasons else "not_ready",
        model_built=model_built,
        upstream_connected=connected,
        reasons=reasons,
    )


@router.get("/health", response_model=HealthResponse, include_in_schema=False)
async def get_health() -> HealthResponse:
    """Report that the gateway process is serving.

    Deliberately static liveness: probes must not touch the OPC UA connection or the model, so a
    reconnecting server never flips container health. Whether the instance can serve i3X traffic
    is ``/ready``.
    """
    return HealthResponse()


@router.get("/ready", response_model=ReadinessResponse, include_in_schema=False)
async def get_readiness(request: Request) -> JSONResponse:
    """Report whether this instance can serve i3X traffic right now.

    Ready only when the address-space model exists and the upstream connection is Connected, so a
    degraded or skip-connect startup is reported as not ready and an orchestrator stops routing
    traffic to it while the static liveness route keeps the process alive. Probes read the status
    code (``503`` when not ready); the body names the failing conditions. Nothing is logged:
    probes fire every few seconds, and the answer already carries the reason.
    """
    readiness = _readiness(gateway_state(request))
    return JSONResponse(
        status_code=200 if readiness.status == "ready" else 503,
        content=readiness.model_dump(),
    )
