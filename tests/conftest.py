"""
Shared test fixtures and utilities for feature test packages.

Provides:
  - TestClient fixtures (with and without MCP)
  - Fake OPC UA client
  - Test model configuration
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Generator, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from i3x_server.bootstrap.app_factory import create_app
from i3x_server.config.settings import get_settings
from i3x_server.domain.ports.opcua import DataValueDTO, OpcUaRuntimeMetrics, OpcUaValue
from i3x_server.infrastructure.opcua.client import (
    OpcUaConnectionSnapshot,
    OpcUaNamespaceInfo,
    OpcUaOperationalLimits,
    OpcUaRequestMetrics,
    OpcUaSubscriptionCapabilities,
)
from i3x_server.infrastructure.subscriptions.service import SubscriptionService
from i3x_server.schemas.i3x import ModelNode, NodeKind
from i3x_server.schemas.opcua_value import quality_from_status_code
from i3x_server.schemas.state import BuildResult

# Numeric OPC UA codes for the i3X quality vocabulary. The DTO carries both the code
# (severity mapping) and the adapter-decoded quality (keeps GoodNoData distinct), so the
# fakes answer exactly like the adapter conversion does.
_QUALITY_STATUS_CODES = {
    "Good": 0x00000000,
    "GoodNoData": 0x00020000,
    "Uncertain": 0x40000000,
    "Bad": 0x80000000,
}

# ``DataValueDTO.source_timestamp`` is optional, so a sort key over it is not total. Every
# history record the fake writes carries a timezone-aware timestamp; this fallback only keeps
# the comparison total (and aware, never naive) if a record without one ever slips in.
_HISTORY_SORT_FALLBACK = datetime.max.replace(tzinfo=timezone.utc)


def data_value_dto(value: Any, quality: str = "Good", timestamp: datetime | None = None) -> DataValueDTO:
    """Build a data-value DTO like the adapter would hand over for ``value``/``quality``."""
    status_code = _QUALITY_STATUS_CODES[quality]
    return DataValueDTO(
        value=OpcUaValue(value=value),
        status_code=status_code,
        quality=quality_from_status_code(SimpleNamespace(value=status_code)),
        source_timestamp=timestamp,
    )


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
    # Mirror the production wiring (bootstrap.app_factory): the admission caps come from the
    # environment, so API-level tests toggle ``I3X_SUBSCRIPTION_MAX_COUNT`` /
    # ``I3X_SUBSCRIPTION_SUBTREE_MAX_NODES`` exactly like a deployed gateway does.
    runtime = get_settings()
    app.state.subscription_service = SubscriptionService(
        app.state.opcua_client,
        interval_seconds=1,
        max_subscriptions=runtime.subscription_max_count,
        subtree_max_nodes=runtime.subscription_subtree_max_nodes,
        native_max_bins=runtime.subscription_native_max_bins,
        native_max_monitored_items=runtime.subscription_native_max_monitored_items,
    )


def _append_unique(items: list[str], *values: str) -> None:
    for value in values:
        if value not in items:
            items.append(value)


def add_composition_depth_chain(app: FastAPI) -> dict[str, str]:
    """Attach a three-level composition chain plus a hierarchy-only branch to the test model.

    Composition plane (``HasComponent``): ``asset-root`` -> ``comp-prop`` (level 1),
    ``asset-root`` -> ``comp-child`` -> ``mid-prop`` (level 2), ``comp-child`` -> ``sub-comp``
    -> ``deep-prop`` (level 3). Hierarchy plane (``HasChildren``): ``asset-root`` ->
    ``hier-child`` -> ``hier-prop`` - those must never be reached by value/history recursion
    (i3X spec IMPLEMENTATION_GUIDE.md:1111).
    """
    model = app.state.model_cache
    chain: dict[str, tuple[str, NodeKind, str]] = {
        "comp-prop": ("CompositionProp", "property", "ns=2;s=CompositionProp"),
        "comp-child": ("CompChild", "asset", "ns=2;s=CompChild"),
        "mid-prop": ("MidProp", "property", "ns=2;s=MidProp"),
        "sub-comp": ("SubComp", "asset", "ns=2;s=SubComp"),
        "deep-prop": ("DeepProp", "property", "ns=2;s=DeepProp"),
        "hier-child": ("HierChild", "asset", "ns=2;s=HierChild"),
        "hier-prop": ("HierProp", "property", "ns=2;s=HierProp"),
    }
    for node_id, (name, kind, source_node_id) in chain.items():
        model.nodes_by_id[node_id] = ModelNode(
            id=node_id,
            name=name,
            kind=kind,
            type=None,
            children=[],
            source_node_id=source_node_id,
        )
        model.children_by_id[node_id] = []

    _append_unique(model.children_by_id.setdefault("asset-root", []), "comp-prop", "comp-child", "hier-child")
    model.children_by_id["comp-child"] = ["mid-prop", "sub-comp"]
    model.children_by_id["sub-comp"] = ["deep-prop"]
    model.children_by_id["hier-child"] = ["hier-prop"]

    composition = model.composition_children_by_id
    _append_unique(composition.setdefault("asset-root", []), "comp-prop", "comp-child")
    composition["comp-child"] = ["mid-prop", "sub-comp"]
    composition["sub-comp"] = ["deep-prop"]
    composition["hier-child"] = ["hier-prop"]

    hierarchy = model.hierarchy_children_by_id
    _append_unique(hierarchy.setdefault("asset-root", []), "comp-prop", "comp-child", "hier-child")
    hierarchy["hier-child"] = ["hier-prop"]

    client = app.state.opcua_client
    for node_id, (_name, kind, source_node_id) in chain.items():
        if kind != "property":
            continue
        model.property_to_node[node_id] = source_node_id
        client.values.setdefault(source_node_id, 10.0)
        client.history_values.setdefault(source_node_id, []).append(
            data_value_dto(10.0, "Good", datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc))
        )
    return {node_id: node_id for node_id in chain}


class FakeOpcUaClient:
    """Fake OPC UA client for testing."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {"ns=2;s=Temperature": 42.5}
        self.writable_by_node_id: dict[str, bool] = {"ns=2;s=Temperature": True}
        self.user_writable_by_node_id: dict[str, bool] = {"ns=2;s=Temperature": True}
        self.variant_type_by_node_id: dict[str, str] = {"ns=2;s=Temperature": "Double"}
        self.last_write_vqt_by_node_id: dict[str, tuple[str, datetime]] = {}
        self.write_failures: dict[str, Exception] = {}
        self.history_writable_by_node_id: dict[str, bool] = {"ns=2;s=Temperature": True}
        self.history_user_writable_by_node_id: dict[str, bool] = {"ns=2;s=Temperature": True}
        self.history_write_failures: dict[str, Exception] = {}
        self.history_update_support: bool | None = None
        self.history_values: dict[str, list[DataValueDTO]] = {
            "ns=2;s=Temperature": [
                data_value_dto(40.0, "Good", datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)),
                data_value_dto(41.5, "Good", datetime(2026, 1, 1, 10, 5, tzinfo=timezone.utc)),
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
        self._runtime_metrics = OpcUaRuntimeMetrics()
        self._operational_limits = OpcUaOperationalLimits(
            max_nodes_per_browse=1000,
            max_nodes_per_read=1000,
        )

    async def get_namespace_infos(self) -> list[OpcUaNamespaceInfo]:
        return [
            OpcUaNamespaceInfo(
                uri="http://example.com/i3x",
                display_name="I3X",
                namespace_version="1.0",
                server_uri="urn:test-server",
            ),
            OpcUaNamespaceInfo(
                uri="http://example.com/custom",
                display_name="Custom",
                namespace_version="1.0",
                server_uri="urn:test-server",
            ),
            OpcUaNamespaceInfo(
                uri="http://example.com/runtime",
                display_name="Runtime",
                namespace_version="1.0",
                server_uri="urn:test-server",
            ),
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
        if node_id == "nsu=http://example.com/custom;i=900001":
            return "CustomNetworkAddressType"
        if node_id == "nsu=http://example.com/custom;i=900002":
            return "CustomMachineStateType"
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

    async def read_data_values(self, node_ids: list[str]) -> list[DataValueDTO]:
        self._reads += 1
        results: list[DataValueDTO] = []
        for node_id in node_ids:
            base = self.values.get(node_id, 1.0)
            # Mirrors a server that preserves the last written VQT: the bulk write path
            # verifies quality and source timestamp through this batched read. Written
            # values are answered verbatim (no simulated drift), so a verification read
            # does not alter the stored value.
            written = self.last_write_vqt_by_node_id.get(node_id)
            if written is not None:
                quality, source_timestamp = written
                value = bytes(base) if isinstance(base, (bytes, bytearray, memoryview)) else base
                results.append(data_value_dto(value, quality, source_timestamp))
                continue
            read_value: Any
            if isinstance(base, (bytes, bytearray, memoryview)):
                read_value = bytes(base)
            else:
                read_value = float(base) + self._reads
                self.values[node_id] = read_value
            results.append(data_value_dto(read_value, "Good", datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)))
        return results

    async def read_history_values(
        self,
        node_ids: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> dict[str, list[DataValueDTO]]:
        del start_time, end_time
        return {node_id: self.history_values.get(node_id, []) for node_id in node_ids}

    async def read_write_access(self, node_id: str) -> tuple[bool, bool]:
        return self.writable_by_node_id.get(node_id, False), self.user_writable_by_node_id.get(node_id, False)

    async def read_variant_type(self, node_id: str) -> str | None:
        return self.variant_type_by_node_id.get(node_id)

    async def read_history_write_access(self, node_id: str) -> tuple[bool, bool]:
        return (
            self.history_writable_by_node_id.get(node_id, False),
            self.history_user_writable_by_node_id.get(node_id, False),
        )

    def history_update_supported(self) -> bool | None:
        return self.history_update_support

    async def write_history_value(self, node_id: str, value: Any, quality: str, timestamp: datetime) -> None:
        failure = self.history_write_failures.get(node_id)
        if failure is not None:
            self._request_metrics.failed_request_count += 1
            raise failure
        variant_type = self.variant_type_by_node_id.get(node_id)
        if variant_type == "Double" and value is not None:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError("Value does not conform to OPC UA Double")
        records = self.history_values.setdefault(node_id, [])
        records[:] = [record for record in records if record.source_timestamp != timestamp]
        records.append(data_value_dto(value, quality, timestamp))
        records.sort(key=lambda record: record.source_timestamp or _HISTORY_SORT_FALLBACK)
        self._request_metrics.history_write_count += 1

    async def write_value(
        self,
        node_id: str,
        value: Any,
        variant_type: str | None,
        quality: str,
        timestamp: datetime,
        verify_metadata: bool = True,
    ) -> None:
        del variant_type, verify_metadata
        failure = self.write_failures.get(node_id)
        if failure is not None:
            self._request_metrics.failed_request_count += 1
            raise failure
        self.values[node_id] = value
        self.last_write_vqt_by_node_id[node_id] = (quality, timestamp)
        self._request_metrics.write_count += 1

    async def read_server_status_data_value(self) -> DataValueDTO:
        return data_value_dto(
            {
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
            },
            "Good",
            datetime(2026, 1, 1, 9, 3, 47, tzinfo=timezone.utc),
        )

    def get_connection_snapshot(self) -> OpcUaConnectionSnapshot:
        return self._connection_snapshot

    def snapshot_request_metrics(self) -> OpcUaRequestMetrics:
        return self._request_metrics

    def reset_runtime_metrics(self) -> None:
        self._runtime_metrics = OpcUaRuntimeMetrics()

    def snapshot_runtime_metrics(self) -> OpcUaRuntimeMetrics:
        # Mirrors the adapter: a copy, so a logged snapshot cannot mutate live counters.
        return replace(self._runtime_metrics)

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

    async def browse_tree(self) -> list[Any]:
        # The model is built from the fixture's static node table, not from a tree walk.
        return []

    async def get_namespaces(self) -> list[str]:
        return [info.uri for info in await self.get_namespace_infos()]

    async def resolve_type_definition_hierarchy(self, type_definition_id: str) -> list[Any]:
        del type_definition_id
        return []

    async def resolve_type_definition_hierarchies(self, type_definition_ids: list[str]) -> dict[str, list[Any]]:
        return {type_definition_id: [] for type_definition_id in type_definition_ids}

    async def create_datachange_subscription(self, publishing_interval_ms: float, handler: Any) -> Any:
        del publishing_interval_ms, handler
        return SimpleNamespace(delete=self._noop_async)

    async def subscribe_data_changes(self, subscription: Any, node_ids: list[str]) -> list[int]:
        del subscription
        return list(range(len(node_ids)))

    async def unsubscribe_data_changes(self, subscription: Any, handles: list[int]) -> None:
        del subscription, handles

    async def delete_subscription(self, subscription: Any) -> None:
        await self._noop_async()

    def get_monitored_item_handles(self, subscription: Any, node_ids: list[str]) -> dict[str, int] | None:
        del subscription, node_ids
        return None

    def add_reconnect_listener(self, listener: Any) -> None:
        self._listeners.append(listener)

    def remove_reconnect_listener(self, listener: Any) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    async def call_method(self, object_node_id: str, method_node_id: str, args: list[Any]) -> Any:
        return {"object": object_node_id, "method": method_node_id, "args": args}

    async def _noop_async(self) -> None:
        return None


_MCP_ENABLED_ENV = {
    "I3X_ENABLE_MCP": "1",
    "I3X_SKIP_OPCUA_CONNECT": "1",
    "I3X_ENABLE_WRITES": "0",
    "I3X_MCP_INCLUDE_OPCUA_METADATA": "1",
}
# Explicitly disabled: unsetting would let a .env in the working directory enable MCP.
_MCP_DISABLED_ENV = {**_MCP_ENABLED_ENV, "I3X_ENABLE_MCP": "0"}


def _test_client_with_env(
    monkeypatch: pytest.MonkeyPatch,
    env: Mapping[str, str],
    configure: Callable[[FastAPI], None],
) -> Generator[TestClient, None, None]:
    """Build an app with the given environment.

    `monkeypatch` restores the variables even when `create_app()` raises, which manual
    `os.environ` mutation would leak into later tests (L12).
    """
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    app = create_app()
    with TestClient(app) as test_client:
        configure(app)
        yield test_client


@contextlib.contextmanager
def _client_with_env(monkeypatch: pytest.MonkeyPatch, extra_env: Mapping[str, str]) -> Iterator[TestClient]:
    """Test client on the standard MCP env plus ``extra_env`` overrides.

    Use this for behaviour that is toggled by an ``I3X_*`` variable; the shared MCP env keeps
    the fixture's other defaults intact.
    """
    yield from _test_client_with_env(monkeypatch, {**_MCP_ENABLED_ENV, **extra_env}, configure_test_app)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    """Test client fixture with MCP enabled."""
    yield from _test_client_with_env(monkeypatch, _MCP_ENABLED_ENV, configure_test_app)


@pytest.fixture
def client_without_mcp(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    """Test client fixture with MCP disabled."""
    yield from _test_client_with_env(monkeypatch, _MCP_DISABLED_ENV, configure_test_app)


@pytest.fixture
def client_without_tool_overrides(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    """Test client fixture with MCP tool overrides disabled."""
    monkeypatch.setattr(
        "i3x_server.mcp.tool_definitions.load_overrides",
        lambda *args, **kwargs: {"tools": {}, "prompts": {}, "excluded_tools": []},
    )
    yield from _test_client_with_env(monkeypatch, _MCP_ENABLED_ENV, configure_test_app)


def assert_documented_response(client: TestClient, method: str, path: str, status: int, payload: Any) -> None:
    """Validate a runtime payload against the response schema documented in ``/openapi.json``."""
    document = client.get("/openapi.json").json()
    schema = document["paths"][path][method]["responses"][str(status)]["content"]["application/json"]["schema"]
    Draft202012Validator(
        {**schema, "components": document["components"]},
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    ).validate(payload)
