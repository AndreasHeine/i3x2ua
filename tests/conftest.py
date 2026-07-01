"""
Shared test fixtures and utilities for feature test packages.

Provides:
  - TestClient fixtures (with and without MCP)
  - Fake OPC UA client
  - Test model configuration
"""

from __future__ import annotations

import os
from collections.abc import Generator
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from i3x_server.bootstrap.app_factory import create_app
from i3x_server.infrastructure.opcua.client import (
    OpcUaConnectionSnapshot,
    OpcUaNamespaceInfo,
    OpcUaOperationalLimits,
    OpcUaRequestMetrics,
    OpcUaSubscriptionCapabilities,
)
from i3x_server.infrastructure.subscriptions.service import SubscriptionService
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult


@dataclass(slots=True)
class FakeMachineThresholds:
    min: float
    max: float


@dataclass(slots=True)
class FakeMachineConfig:
    thresholds: FakeMachineThresholds
    mode: str


class FakeExtensionObject:
    def __init__(self, type_id: str, body: Any) -> None:
        self.TypeId = type_id
        self.Body = body


def fastapi_app(client: TestClient) -> FastAPI:
    """Extract FastAPI app from test client."""
    return cast(FastAPI, client.app)


def configure_test_app(app: FastAPI) -> None:
    """Configure test app with mock model and OPC UA client."""
    property_id = "property-abc"
    action_id = "action-def"
    root_id = "asset-root"

    app.state.model_cache = BuildResult(
        nodes_by_id={
            root_id: ModelNode(
                id=root_id,
                name="Machine",
                kind="asset",
                type=None,
                children=[property_id, action_id],
                source_node_id="ns=2;s=Machine",
                source_type_id="ns=1;i=1001",
            ),
            "sensor-root": ModelNode(
                id="sensor-root",
                name="Sensor",
                kind="asset",
                type=None,
                children=[],
                source_node_id="ns=2;s=Sensor",
                source_type_id="ns=1;i=1002",
            ),
            property_id: ModelNode(
                id=property_id,
                name="Temperature",
                kind="property",
                type="ns=1;i=11",
                children=[],
                source_node_id="ns=2;s=Temperature",
            ),
            action_id: ModelNode(
                id=action_id,
                name="Reset",
                kind="action",
                type=None,
                children=[],
                source_node_id="ns=2;s=Reset",
            ),
        },
        root_ids=[root_id, "sensor-root"],
        children_by_id={root_id: [property_id, action_id], "sensor-root": [], property_id: [], action_id: []},
        instances_by_type_id={"ns=1;i=1001": [root_id], "ns=1;i=1002": ["sensor-root"]},
        property_to_node={property_id: "ns=2;s=Temperature"},
        action_to_method={action_id: ("ns=2;s=Machine", "ns=2;s=Reset")},
    )
    app.state.opcua_client = FakeOpcUaClient()
    app.state.subscription_service = SubscriptionService(app.state.opcua_client, interval_seconds=1)


class FakeOpcUaClient:
    """Fake OPC UA client for testing."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {"ns=2;s=Temperature": 42.5}
        self.writable_by_node_id: dict[str, bool] = {"ns=2;s=Temperature": True}
        self.user_writable_by_node_id: dict[str, bool] = {"ns=2;s=Temperature": True}
        self.variant_type_by_node_id: dict[str, str] = {"ns=2;s=Temperature": "Double"}
        self.write_failures: dict[str, Exception] = {}
        self.history_values: dict[str, list[SimpleNamespace]] = {
            "ns=2;s=Temperature": [
                SimpleNamespace(
                    Value=SimpleNamespace(Value=40.0),
                    StatusCode=SimpleNamespace(name="Good"),
                    SourceTimestamp=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
                    ServerTimestamp=None,
                ),
                SimpleNamespace(
                    Value=SimpleNamespace(Value=41.5),
                    StatusCode=SimpleNamespace(name="Good"),
                    SourceTimestamp=datetime(2026, 1, 1, 10, 5, tzinfo=timezone.utc),
                    ServerTimestamp=None,
                ),
            ]
        }
        self._reads = 0
        self._listeners: list[Any] = []
        self._connection_snapshot = OpcUaConnectionSnapshot(
            state="Connected",
            endpoint="opc.tcp://opcua.umati.app:4843",
            since=datetime(2026, 1, 1, 8, 11, 24, tzinfo=timezone.utc),
        )
        self._request_metrics = OpcUaRequestMetrics(
            read_count=2451,
            write_count=0,
            browse_count=189,
            method_call_count=27,
            history_read_count=62,
            history_write_count=0,
            failed_request_count=14,
            goodish_qualities=["Good", "Uncertain"],
        )
        self._operational_limits = OpcUaOperationalLimits(
            max_nodes_per_browse=1000,
            max_nodes_per_read=1000,
        )

    async def get_namespace_infos(self) -> list[OpcUaNamespaceInfo]:
        return [
            OpcUaNamespaceInfo(uri="http://example.com/i3x", display_name="I3X"),
            OpcUaNamespaceInfo(uri="http://example.com/custom", display_name="Custom"),
            OpcUaNamespaceInfo(uri="http://example.com/runtime", display_name="Runtime"),
        ]

    async def get_object_types(self) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                node_id="ns=1;i=1001",
                parent_node_id=None,
                browse_name="MachineType",
                display_name="Machine Type",
                description="Machine object type",
                is_abstract=False,
                properties={
                    "temperature": "i=11",
                    "running": "i=1",
                    "config": "ns=1;i=3001",
                },
                members=[
                    SimpleNamespace(
                        node_id="ns=1;i=2001",
                        browse_name="temperature",
                        display_name="Temperature",
                        description="Current measured temperature",
                        node_class="Variable",
                        data_type="i=11",
                        value=42.5,
                        modelling_rule="Mandatory",
                    ),
                    SimpleNamespace(
                        node_id="ns=1;i=2002",
                        browse_name="running",
                        display_name="Running",
                        description="Running state",
                        node_class="Variable",
                        data_type="i=1",
                        value=True,
                        modelling_rule=None,
                    ),
                    SimpleNamespace(
                        node_id="ns=1;i=2003",
                        browse_name="config",
                        display_name="Config",
                        description="Machine configuration",
                        node_class="Variable",
                        data_type="ns=1;i=3001",
                        value=FakeExtensionObject(
                            "ns=1;i=3001",
                            FakeMachineConfig(
                                thresholds=FakeMachineThresholds(min=10.0, max=120.5),
                                mode="auto",
                            ),
                        ),
                        modelling_rule=None,
                    ),
                ],
            ),
            SimpleNamespace(
                node_id="ns=1;i=1002",
                parent_node_id="ns=1;i=1001",
                browse_name="SensorType",
                display_name="Sensor Type",
                description="Sensor subtype",
                is_abstract=True,
                properties={},
                members=[],
            ),
        ]

    async def read_value(self, node_id: str) -> Any:
        return self.values[node_id]

    async def read_browse_name(self, node_id: str) -> str | None:
        if node_id == "ns=0;i=17364":
            return "NetworkAddressDataType"
        if node_id == "ns=0;i=865":
            return "SessionDiagnosticsDataType"
        if node_id == "ns=0;i=96":
            return "RolePermissionType"
        return None

    async def read_values(self, node_ids: list[str]) -> list[Any]:
        self._reads += 1
        results: list[Any] = []
        for node_id in node_ids:
            base = self.values.get(node_id, 1.0)
            if isinstance(base, (bytes, bytearray, memoryview)):
                results.append(bytes(base))
                continue
            value = float(base) + self._reads
            self.values[node_id] = value
            results.append(value)
        return results

    async def read_data_values(self, node_ids: list[str]) -> list[Any]:
        self._reads += 1
        results: list[Any] = []
        for node_id in node_ids:
            base = self.values.get(node_id, 1.0)
            value: Any
            if isinstance(base, (bytes, bytearray, memoryview)):
                value = bytes(base)
            else:
                value = float(base) + self._reads
                self.values[node_id] = value
            results.append(
                SimpleNamespace(
                    Value=SimpleNamespace(Value=value),
                    StatusCode=SimpleNamespace(name="Good", is_good=lambda: True),
                    SourceTimestamp=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                    ServerTimestamp=None,
                )
            )
        return results

    async def read_history_values(
        self,
        node_ids: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> dict[str, list[Any]]:
        del start_time, end_time
        return {node_id: self.history_values.get(node_id, []) for node_id in node_ids}

    async def read_write_access(self, node_id: str) -> tuple[bool, bool]:
        return self.writable_by_node_id.get(node_id, False), self.user_writable_by_node_id.get(node_id, False)

    async def read_variant_type(self, node_id: str) -> str | None:
        return self.variant_type_by_node_id.get(node_id)

    async def write_value(self, node_id: str, value: Any) -> None:
        failure = self.write_failures.get(node_id)
        if failure is not None:
            self._request_metrics.failed_request_count += 1
            raise failure
        self.values[node_id] = value
        self._request_metrics.write_count += 1

    async def read_server_status_data_value(self) -> Any:
        return SimpleNamespace(
            Value=SimpleNamespace(
                Value={
                    "StartTime": datetime(2026, 1, 1, 8, 11, 22, tzinfo=timezone.utc),
                    "CurrentTime": datetime(2026, 1, 1, 9, 3, 47, tzinfo=timezone.utc),
                    "State": "Running",
                    "BuildInfo": {
                        "ProductUri": "urn:vendor:server",
                        "ManufacturerName": "Vendor",
                        "ProductName": "UA Server",
                        "SoftwareVersion": "1.2.3",
                        "BuildNumber": "20260613",
                        "BuildDate": datetime(2026, 1, 1, 7, 59, 0, tzinfo=timezone.utc),
                    },
                    "SecondsTillShutdown": 0,
                    "ShutdownReason": "",
                }
            ),
            StatusCode=SimpleNamespace(name="Good", is_good=lambda: True, is_uncertain=lambda: False),
            SourceTimestamp=datetime(2026, 1, 1, 9, 3, 47, tzinfo=timezone.utc),
            ServerTimestamp=None,
        )

    def get_connection_snapshot(self) -> OpcUaConnectionSnapshot:
        return self._connection_snapshot

    def snapshot_request_metrics(self) -> OpcUaRequestMetrics:
        return self._request_metrics

    async def get_operational_limits(self) -> OpcUaOperationalLimits:
        return self._operational_limits

    async def get_subscription_capabilities(self) -> OpcUaSubscriptionCapabilities:
        return OpcUaSubscriptionCapabilities(
            max_monitored_items_per_call=1,
            max_subscriptions=100,
            max_monitored_items=100,
            max_subscriptions_per_session=100,
            max_monitored_items_per_subscription=1,
        )

    async def create_datachange_subscription(self, publishing_interval_ms: float, handler: Any) -> Any:
        return SimpleNamespace(delete=self._noop_async)

    async def subscribe_data_changes(self, subscription: Any, node_ids: list[str]) -> list[int]:
        return list(range(len(node_ids)))

    async def delete_subscription(self, subscription: Any) -> None:
        await self._noop_async()

    def add_reconnect_listener(self, listener: Any) -> None:
        self._listeners.append(listener)

    async def call_method(self, object_node_id: str, method_node_id: str, args: list[Any]) -> Any:
        return {"object": object_node_id, "method": method_node_id, "args": args}

    async def _noop_async(self) -> None:
        return None


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """Test client fixture with MCP enabled."""
    previous_enable_mcp = os.environ.get("I3X_ENABLE_MCP")
    previous_skip_connect = os.environ.get("I3X_SKIP_OPCUA_CONNECT")
    os.environ["I3X_ENABLE_MCP"] = "1"
    os.environ["I3X_SKIP_OPCUA_CONNECT"] = "1"
    app = create_app()
    try:
        with TestClient(app) as test_client:
            configure_test_app(app)
            yield test_client
    finally:
        if previous_enable_mcp is None:
            os.environ.pop("I3X_ENABLE_MCP", None)
        else:
            os.environ["I3X_ENABLE_MCP"] = previous_enable_mcp
        if previous_skip_connect is None:
            os.environ.pop("I3X_SKIP_OPCUA_CONNECT", None)
        else:
            os.environ["I3X_SKIP_OPCUA_CONNECT"] = previous_skip_connect


@pytest.fixture
def client_without_mcp() -> Generator[TestClient, None, None]:
    """Test client fixture with MCP disabled."""
    previous_enable_mcp = os.environ.get("I3X_ENABLE_MCP")
    previous_skip_connect = os.environ.get("I3X_SKIP_OPCUA_CONNECT")
    os.environ.pop("I3X_ENABLE_MCP", None)
    os.environ["I3X_SKIP_OPCUA_CONNECT"] = "1"
    app = create_app()
    try:
        with TestClient(app) as test_client:
            configure_test_app(app)
            yield test_client
    finally:
        if previous_enable_mcp is None:
            os.environ.pop("I3X_ENABLE_MCP", None)
        else:
            os.environ["I3X_ENABLE_MCP"] = previous_enable_mcp
        if previous_skip_connect is None:
            os.environ.pop("I3X_SKIP_OPCUA_CONNECT", None)
        else:
            os.environ["I3X_SKIP_OPCUA_CONNECT"] = previous_skip_connect


@pytest.fixture
def client_without_tool_overrides(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    """Test client fixture with MCP tool overrides disabled."""
    previous_enable_mcp = os.environ.get("I3X_ENABLE_MCP")
    previous_skip_connect = os.environ.get("I3X_SKIP_OPCUA_CONNECT")
    os.environ["I3X_ENABLE_MCP"] = "1"
    os.environ["I3X_SKIP_OPCUA_CONNECT"] = "1"
    monkeypatch.setattr("i3x_server.mcp.load_tool_overrides", lambda *args, **kwargs: {})
    app = create_app()
    try:
        with TestClient(app) as test_client:
            configure_test_app(app)
            yield test_client
    finally:
        if previous_enable_mcp is None:
            os.environ.pop("I3X_ENABLE_MCP", None)
        else:
            os.environ["I3X_ENABLE_MCP"] = previous_enable_mcp
        if previous_skip_connect is None:
            os.environ.pop("I3X_SKIP_OPCUA_CONNECT", None)
        else:
            os.environ["I3X_SKIP_OPCUA_CONNECT"] = previous_skip_connect
