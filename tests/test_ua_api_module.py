from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from i3x_server.api import (
    _iso_timestamp,
    _to_json_safe,
    get_ua_connection,
    get_ua_limits,
    get_ua_metrics,
    get_ua_state,
)
from i3x_server.opcua.contracts import (
    OpcUaOperationalLimits,
    OpcUaRequestMetrics,
    OpcUaSubscriptionCapabilities,
)


@dataclass
class _DataclassValue:
    name: str
    created_at: datetime


class _ObjectValue:
    def __init__(self) -> None:
        self.value = 7
        self._private = "hidden"


def test_iso_timestamp_for_aware_naive_and_fallback() -> None:
    aware = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    naive = datetime(2026, 1, 1, 0, 0)
    assert _iso_timestamp(aware).endswith("Z")
    assert _iso_timestamp(naive).endswith("Z")
    assert _iso_timestamp("invalid").endswith("Z")


def test_to_json_safe_handles_core_types_and_nested_structures() -> None:
    payload = {
        "primitive": 1,
        "datetime": datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        "dataclass": _DataclassValue("demo", datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)),
        "iterables": ({"k": "v"}, [1, 2], {3, 4}),
        "object": _ObjectValue(),
    }
    safe = _to_json_safe(payload)
    assert safe["primitive"] == 1
    assert isinstance(safe["datetime"], str) and safe["datetime"].endswith("Z")
    assert safe["dataclass"]["name"] == "demo"
    assert safe["object"]["value"] == 7
    assert "_private" not in safe["object"]


@pytest.mark.asyncio
async def test_get_ua_state_success_and_error() -> None:
    value = SimpleNamespace(Value=SimpleNamespace(Value={"state": "RUNNING"}))

    class GoodClient:
        async def read_server_status_data_value(self) -> SimpleNamespace:
            return value

    success = await get_ua_state(opcua_client=GoodClient())
    assert success.result == {"state": "RUNNING"}

    class FailingClient:
        async def read_server_status_data_value(self) -> SimpleNamespace:
            raise RuntimeError("failed")

    with pytest.raises(HTTPException) as exc_info:
        await get_ua_state(opcua_client=FailingClient())
    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_get_ua_connection_success_and_error() -> None:
    snapshot = SimpleNamespace(
        state="connected",
        endpoint="opc.tcp://localhost:4840",
        since=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
    )

    class GoodClient:
        def get_connection_snapshot(self) -> SimpleNamespace:
            return snapshot

    success = await get_ua_connection(opcua_client=GoodClient())
    assert success.result.state == "connected"
    assert success.result.since.endswith("Z")

    class FailingClient:
        def get_connection_snapshot(self) -> SimpleNamespace:
            raise RuntimeError("failed")

    with pytest.raises(HTTPException) as exc_info:
        await get_ua_connection(opcua_client=FailingClient())
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_get_ua_limits_success_and_error() -> None:
    class GoodClient:
        async def get_operational_limits(self) -> OpcUaOperationalLimits:
            return OpcUaOperationalLimits(max_nodes_per_browse=100, max_nodes_per_read=200)

        async def get_subscription_capabilities(self) -> OpcUaSubscriptionCapabilities:
            return OpcUaSubscriptionCapabilities(
                max_monitored_items_per_call=10,
                max_subscriptions=20,
                max_monitored_items=30,
                max_subscriptions_per_session=40,
                max_monitored_items_per_subscription=50,
            )

    success = await get_ua_limits(opcua_client=GoodClient())
    assert success.result.operationalLimits.maxNodesPerBrowse == 100
    assert success.result.serverCapabilities.maxSubscriptionsPerSession == 40

    class FailingClient:
        async def get_operational_limits(self) -> OpcUaOperationalLimits:
            raise RuntimeError("failed")

        async def get_subscription_capabilities(self) -> OpcUaSubscriptionCapabilities:
            raise RuntimeError("failed")

    with pytest.raises(HTTPException) as exc_info:
        await get_ua_limits(opcua_client=FailingClient())
    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_get_ua_metrics_maps_fields() -> None:
    class Client:
        def snapshot_request_metrics(self) -> OpcUaRequestMetrics:
            return OpcUaRequestMetrics(
                read_count=1,
                write_count=2,
                browse_count=3,
                method_call_count=4,
                history_read_count=5,
                history_write_count=6,
                failed_request_count=7,
                goodish_qualities=["Good", "Uncertain"],
            )

    result = await get_ua_metrics(opcua_client=Client())
    assert result.result.readCount == 1
    assert result.result.failedRequestCount == 7
    assert result.result.goodishQualities == ["Good", "Uncertain"]
