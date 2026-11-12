from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from time import monotonic
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from i3x_server.api.v1.contracts import HttpErrorResponse
from i3x_server.application.ports.opcua import OpcUaClientProtocol
from i3x_server.bootstrap.dependencies import get_opcua_client
from i3x_server.errors import i3x_http_error

router = APIRouter(prefix="/ua", tags=["Diagnostics"])

# The /view page polls /ua/status every couple of seconds, and ServerStatus is a live
# OPC UA round-trip. The struct is therefore cached briefly: only CurrentTime moves
# between reads, so serving the snapshot for a few seconds is a small, bounded staleness
# instead of a server round-trip per poll.
_SERVER_STATUS_TTL_SECONDS = 3.0


class UaSuccessResponse(BaseModel):
    success: bool = True
    result: Any = None


class UaConnectionResult(BaseModel):
    state: str
    endpoint: str
    since: str
    # Readiness signal for orchestrators: `/health` is deliberately static (container liveness
    # must not depend on OPC UA), so upstream readiness is reported here instead.
    ready: bool
    startupMode: str


class UaOperationalLimitsResult(BaseModel):
    maxNodesPerBrowse: int | None
    maxNodesPerRead: int | None


class UaServerCapabilitiesResult(BaseModel):
    maxMonitoredItemsPerCall: int | None
    maxSubscriptions: int | None
    maxMonitoredItems: int | None
    maxSubscriptionsPerSession: int | None
    maxMonitoredItemsPerSubscription: int | None


class UaLimitsResult(BaseModel):
    operationalLimits: UaOperationalLimitsResult
    serverCapabilities: UaServerCapabilitiesResult


class UaMetricsResult(BaseModel):
    goodishQualities: list[str] = Field(default_factory=list)
    readCount: int
    writeCount: int
    browseCount: int
    methodCallCount: int
    historyReadCount: int
    historyWriteCount: int
    failedRequestCount: int


def _iso_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _to_json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return _iso_timestamp(value)
    if is_dataclass(value):
        return {item.name: _to_json_safe(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, dict):
        return {str(key): _to_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_json_safe(item) for item in value]
    if hasattr(value, "__dict__") and type(value).__module__ != "builtins":
        return {
            str(key): _to_json_safe(item)
            for key, item in vars(value).items()
            if not key.startswith("_") and not callable(item)
        }
    return str(value)


class _ServerStatusSnapshot:
    """Last successful ServerStatus read plus when it was taken and on which connection."""

    __slots__ = ("connection_state", "fetched_at", "payload")

    def __init__(self, payload: Any, connection_state: str, fetched_at: float) -> None:
        self.payload = payload
        self.connection_state = connection_state
        self.fetched_at = fetched_at


def _connection_state(opcua_client: OpcUaClientProtocol) -> str:
    """Local connection state used to invalidate the cached ServerStatus after a reconnect."""
    snapshot_reader = getattr(opcua_client, "get_connection_snapshot", None)
    if not callable(snapshot_reader):
        return ""
    try:
        return str(snapshot_reader().state)
    except Exception:
        return ""


@router.get(
    "/status",
    response_model=UaSuccessResponse,
    responses={
        502: {"model": HttpErrorResponse, "description": "Failed to read upstream OPC UA ServerStatus."},
    },
)
async def get_ua_status(
    request: Request,
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> UaSuccessResponse:
    connection_state = _connection_state(opcua_client)
    cached = getattr(request.app.state, "ua_status_snapshot", None)
    if (
        isinstance(cached, _ServerStatusSnapshot)
        and cached.connection_state == connection_state
        and monotonic() - cached.fetched_at < _SERVER_STATUS_TTL_SECONDS
    ):
        return UaSuccessResponse(result=cached.payload)

    try:
        data_value = await opcua_client.read_server_status_data_value()
    except Exception as exc:
        raise i3x_http_error(
            502,
            "OpcUaServerStatusError",
            "Failed to read OPC UA ServerStatus",
            {"cause": str(exc)},
        ) from exc

    variant = getattr(data_value, "Value", None)
    payload = getattr(variant, "Value", variant)
    result = _to_json_safe(payload)
    request.app.state.ua_status_snapshot = _ServerStatusSnapshot(result, connection_state, monotonic())
    return UaSuccessResponse(result=result)


@router.get("/connection", response_model=UaSuccessResponse)
async def get_ua_connection(
    request: Request,
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> UaSuccessResponse:
    try:
        snapshot = opcua_client.get_connection_snapshot()
    except Exception as exc:
        raise i3x_http_error(
            500,
            "OpcUaConnectionStateError",
            "Failed to resolve OPC UA connection state",
            {"cause": str(exc)},
        ) from exc

    startup_mode = str(getattr(request.app.state, "ua_startup_mode", "unknown"))
    result = UaConnectionResult(
        state=snapshot.state,
        endpoint=snapshot.endpoint,
        since=_iso_timestamp(snapshot.since),
        ready=snapshot.state == "Connected",
        startupMode=startup_mode,
    )
    return UaSuccessResponse(result=result)


@router.get(
    "/limits",
    response_model=UaSuccessResponse,
    responses={
        502: {"model": HttpErrorResponse, "description": "Failed to read upstream OPC UA limits and capabilities."},
    },
)
async def get_ua_limits(opcua_client: OpcUaClientProtocol = Depends(get_opcua_client)) -> UaSuccessResponse:
    try:
        operational_limits = await opcua_client.get_operational_limits()
        capabilities = await opcua_client.get_subscription_capabilities()
    except Exception as exc:
        raise i3x_http_error(
            502,
            "OpcUaLimitsError",
            "Failed to read OPC UA limits and capabilities",
            {"cause": str(exc)},
        ) from exc

    result = UaLimitsResult(
        operationalLimits=UaOperationalLimitsResult(
            maxNodesPerBrowse=operational_limits.max_nodes_per_browse,
            maxNodesPerRead=operational_limits.max_nodes_per_read,
        ),
        serverCapabilities=UaServerCapabilitiesResult(
            maxMonitoredItemsPerCall=capabilities.max_monitored_items_per_call,
            maxSubscriptions=capabilities.max_subscriptions,
            maxMonitoredItems=capabilities.max_monitored_items,
            maxSubscriptionsPerSession=capabilities.max_subscriptions_per_session,
            maxMonitoredItemsPerSubscription=capabilities.max_monitored_items_per_subscription,
        ),
    )
    return UaSuccessResponse(result=result)


@router.get("/metrics", response_model=UaSuccessResponse)
async def get_ua_metrics(opcua_client: OpcUaClientProtocol = Depends(get_opcua_client)) -> UaSuccessResponse:
    metrics = opcua_client.snapshot_request_metrics()
    result = UaMetricsResult(
        goodishQualities=list(metrics.goodish_qualities),
        readCount=metrics.read_count,
        writeCount=metrics.write_count,
        browseCount=metrics.browse_count,
        methodCallCount=metrics.method_call_count,
        historyReadCount=metrics.history_read_count,
        historyWriteCount=metrics.history_write_count,
        failedRequestCount=metrics.failed_request_count,
    )
    return UaSuccessResponse(result=result)
