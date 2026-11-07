"""Legacy REST/API cases that do not belong to a feature package.

Feature coverage lives in ``tests/features/**`` (docs/architecture.md). The functions
here are the remaining cases without a feature twin: internal serialization helpers,
bulk-update semantics and OpenAPI contract checks. Shared fixtures and the
documented-response validator come from ``tests/conftest.py``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Generator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from i3x_server.api.v1.contracts import MAX_BULK_ITEMS
from i3x_server.api.v1.monolithic import (
    _decode_bytestring_write_value,
    _to_json_safe_value,
)
from i3x_server.bootstrap.app_factory import create_app
from i3x_server.infrastructure.opcua.client import (
    OpcUaConnectionSnapshot,
    OpcUaNamespaceInfo,
    OpcUaOperationalLimits,
    OpcUaRequestMetrics,
    OpcUaSubscriptionCapabilities,
)
from i3x_server.infrastructure.subscriptions.service import SubscriptionService
from i3x_server.mcp import load_overrides, load_tool_overrides
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult
from tests.conftest import assert_documented_response


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


def _contains_opcua_extension_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).startswith("x-opcua-"):
                return True
            if _contains_opcua_extension_key(nested):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_opcua_extension_key(item) for item in value)
    return False


def test_to_json_safe_value_filters_structure_encoding_field() -> None:
    @dataclass(slots=True)
    class _LocalizedText:
        Encoding: int = 0
        Locale: str | None = None
        Text: str | None = None

    serialized = _to_json_safe_value(_LocalizedText(Locale=None, Text="Executing"))
    assert serialized == {"Locale": None, "Text": "Executing"}


def test_to_json_safe_value_extension_object_with_null_body_returns_null() -> None:
    serialized = _to_json_safe_value(FakeExtensionObject("ns=1;i=3001", None))
    assert serialized is None


def test_to_json_safe_value_encodes_bytestring_as_base64_string() -> None:
    assert _to_json_safe_value(b"\xff\x00") == "/wA="


def test_decode_bytestring_write_value_accepts_base64_string() -> None:
    assert _decode_bytestring_write_value("/wA=", "ByteString") == b"\xff\x00"


def test_decode_bytestring_write_value_rejects_invalid_base64_string() -> None:
    with pytest.raises(ValueError, match="Invalid base64 ByteString"):
        _decode_bytestring_write_value("!", "ByteString")


def _fastapi_app(client: TestClient) -> FastAPI:
    return cast(FastAPI, client.app)


def _configure_test_app(app: FastAPI) -> None:
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
    def __init__(self) -> None:
        self.values: dict[str, Any] = {"ns=2;s=Temperature": 42.5}
        self.writable_by_node_id: dict[str, bool] = {"ns=2;s=Temperature": True}
        self.user_writable_by_node_id: dict[str, bool] = {"ns=2;s=Temperature": True}
        self.variant_type_by_node_id: dict[str, str] = {"ns=2;s=Temperature": "Double"}
        self.last_write_vqt_by_node_id: dict[str, tuple[str, datetime]] = {}
        self.last_write_variant_type_by_node_id: dict[str, str | None] = {}
        self.write_failures: dict[str, Exception] = {}
        self.history_update_support: bool | None = None
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
                        parent_node_id="ns=1;i=1001",
                        browse_name="temperature",
                        display_name="Temperature",
                        description="Current measured temperature",
                        node_class="Variable",
                        data_type="i=11",
                        reference_type_id="ns=0;i=46",
                        reference_type="HasProperty",
                        reference_order=0,
                        value=42.5,
                        modelling_rule="Mandatory",
                    ),
                    SimpleNamespace(
                        node_id="ns=1;i=2002",
                        parent_node_id="ns=1;i=1001",
                        browse_name="running",
                        display_name="Running",
                        description="Running state",
                        node_class="Variable",
                        data_type="i=1",
                        reference_type_id="ns=0;i=46",
                        reference_type="HasProperty",
                        reference_order=1,
                        value=True,
                        modelling_rule=None,
                    ),
                    SimpleNamespace(
                        node_id="ns=1;i=2003",
                        parent_node_id="ns=1;i=1001",
                        browse_name="config",
                        display_name="Config",
                        description="Machine configuration",
                        node_class="Variable",
                        data_type="ns=1;i=3001",
                        reference_type_id="ns=0;i=46",
                        reference_type="HasProperty",
                        reference_order=2,
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

    def history_update_supported(self) -> bool | None:
        return self.history_update_support

    async def write_value(
        self,
        node_id: str,
        value: Any,
        variant_type: str | None,
        quality: str,
        timestamp: datetime,
    ) -> None:
        failure = self.write_failures.get(node_id)
        if failure is not None:
            self._request_metrics.failed_request_count += 1
            raise failure
        self.last_write_variant_type_by_node_id[node_id] = variant_type
        self.last_write_vqt_by_node_id[node_id] = (quality, timestamp)
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
    previous_enable_mcp = os.environ.get("I3X_ENABLE_MCP")
    previous_skip_connect = os.environ.get("I3X_SKIP_OPCUA_CONNECT")
    previous_enable_writes = os.environ.get("I3X_ENABLE_WRITES")
    previous_mcp_include_opcua_metadata = os.environ.get("I3X_MCP_INCLUDE_OPCUA_METADATA")
    os.environ["I3X_ENABLE_MCP"] = "1"
    os.environ["I3X_SKIP_OPCUA_CONNECT"] = "1"
    os.environ["I3X_ENABLE_WRITES"] = "0"
    os.environ["I3X_MCP_INCLUDE_OPCUA_METADATA"] = "1"
    app = create_app()
    try:
        with TestClient(app) as test_client:
            _configure_test_app(app)
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
        if previous_enable_writes is None:
            os.environ.pop("I3X_ENABLE_WRITES", None)
        else:
            os.environ["I3X_ENABLE_WRITES"] = previous_enable_writes
        if previous_mcp_include_opcua_metadata is None:
            os.environ.pop("I3X_MCP_INCLUDE_OPCUA_METADATA", None)
        else:
            os.environ["I3X_MCP_INCLUDE_OPCUA_METADATA"] = previous_mcp_include_opcua_metadata


@pytest.fixture
def client_without_mcp() -> Generator[TestClient, None, None]:
    previous_enable_mcp = os.environ.get("I3X_ENABLE_MCP")
    previous_skip_connect = os.environ.get("I3X_SKIP_OPCUA_CONNECT")
    previous_enable_writes = os.environ.get("I3X_ENABLE_WRITES")
    previous_mcp_include_opcua_metadata = os.environ.get("I3X_MCP_INCLUDE_OPCUA_METADATA")
    # Explicitly disabled: unsetting would let a .env in the working directory enable MCP.
    os.environ["I3X_ENABLE_MCP"] = "0"
    os.environ["I3X_SKIP_OPCUA_CONNECT"] = "1"
    os.environ["I3X_ENABLE_WRITES"] = "0"
    os.environ["I3X_MCP_INCLUDE_OPCUA_METADATA"] = "1"
    app = create_app()
    try:
        with TestClient(app) as test_client:
            _configure_test_app(app)
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
        if previous_enable_writes is None:
            os.environ.pop("I3X_ENABLE_WRITES", None)
        else:
            os.environ["I3X_ENABLE_WRITES"] = previous_enable_writes
        if previous_mcp_include_opcua_metadata is None:
            os.environ.pop("I3X_MCP_INCLUDE_OPCUA_METADATA", None)
        else:
            os.environ["I3X_MCP_INCLUDE_OPCUA_METADATA"] = previous_mcp_include_opcua_metadata


def test_v1_info_write_capability_enabled_with_flag(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")

    response = client.get("/v1/info")

    assert response.status_code == 200
    payload = response.json()
    assert payload["result"]["capabilities"]["update"]["current"] is True


def test_v1_objecttypes_opcua_metadata_contract(client: TestClient) -> None:
    """ObjectType schema keeps the OPC UA metadata (data type, references, super type).

    This needs the richer legacy fixture model from ``_configure_test_app`` (object type
    context with ``HasProperty``/``HasSubtype`` references); the shared conftest model has
    no reference context, so the feature-level twin only checks the base fields.
    """
    response = client.get("/v1/objecttypes")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert len(payload["result"]) >= 3
    first = payload["result"][0]
    assert first["elementId"].startswith("urn:") and ":objecttype:" in first["elementId"]
    assert isinstance(first["displayName"], str)
    assert isinstance(first["namespaceUri"], str)
    assert isinstance(first["sourceTypeId"], str)
    assert first["sourceTypeId"].startswith("nsu=http://example.com/custom;")
    assert isinstance(first["schema"], dict)
    assert first["schema"]["type"] == "object"
    assert first["schema"]["x-opcua-nodeId"].startswith("nsu=http://example.com/custom;")
    assert first["schema"]["x-opcua-displayName"] == "Machine Type"
    assert first["schema"]["description"] == "Machine object type"
    assert "x-opcua-description" not in first["schema"]
    assert "x-opcua-references" not in first["schema"]
    assert first["schema"]["x-opcua-isAbstract"] is False
    assert isinstance(first["schema"]["properties"], dict)
    assert first["schema"]["properties"]["temperature"]["type"] == "number"
    assert first["schema"]["properties"]["running"]["type"] == "boolean"
    assert first["schema"]["properties"]["temperature"]["x-opcua-nodeId"].startswith("nsu=http://example.com/custom;")
    assert first["schema"]["properties"]["temperature"]["x-opcua-displayName"] == "Temperature"
    assert first["schema"]["properties"]["temperature"]["description"] == "Current measured temperature"
    assert "x-opcua-description" not in first["schema"]["properties"]["temperature"]
    assert first["schema"]["properties"]["temperature"]["x-opcua-modellingRule"] == "Mandatory"
    assert first["schema"]["properties"]["temperature"]["x-opcua-dataTypeId"] == "nsu=http://opcfoundation.org/UA/;i=11"
    assert first["schema"]["properties"]["temperature"]["x-opcua-referenceType"] == "HasProperty"
    assert first["schema"]["properties"]["temperature"]["x-opcua-referenceOrder"] == 0
    assert (
        first["schema"]["properties"]["temperature"]["x-opcua-referenceTypeId"]
        == "nsu=http://opcfoundation.org/UA/;i=46"
    )
    assert first["schema"]["properties"]["temperature"]["x-opcua-references"][0]["sourceNodeId"] == (
        "nsu=http://example.com/custom;i=1001"
    )
    assert first["schema"]["properties"]["temperature"]["x-opcua-references"][0]["targetNodeId"] == (
        "nsu=http://example.com/custom;i=2001"
    )
    assert first["schema"]["properties"]["temperature"]["x-opcua-value"] == 42.5
    config_schema = first["schema"]["properties"]["config"]
    assert isinstance(config_schema.get("allOf"), list)
    assert isinstance(config_schema["allOf"][0], dict)
    config_ref = config_schema["allOf"][0].get("$ref")
    assert isinstance(config_ref, str) and config_ref.startswith("#/$defs/")
    assert config_schema["x-opcua-displayName"] == "Config"
    config_def_key = config_ref.split("#/$defs/", 1)[1]
    config_def = first["schema"]["$defs"][config_def_key]
    assert config_def["x-opcua-structureTypeId"] == "nsu=http://example.com/custom;i=3001"
    assert config_def["properties"]["mode"]["type"] == "string"
    thresholds_schema = config_def["properties"]["thresholds"]
    assert isinstance(thresholds_schema.get("$ref"), str)
    thresholds_ref = thresholds_schema["$ref"]
    assert thresholds_ref.startswith("#/$defs/")
    thresholds_def_key = thresholds_ref.split("#/$defs/", 1)[1]
    thresholds_def = first["schema"]["$defs"][thresholds_def_key]
    assert thresholds_def["properties"]["min"]["type"] == "number"
    assert thresholds_def["properties"]["max"]["type"] == "number"
    assert isinstance(first["related"], dict)
    assert [item["elementId"] for item in first["related"]["instances"]] == ["asset-root"]
    assert first["related"]["instances"][0]["metadata"]["relationships"]["HasChildren"] == ["action-def"]
    assert first["related"]["instances"][0]["metadata"]["relationships"]["HasComponent"] == ["property-abc"]

    synthetic = next(
        item for item in payload["result"] if item["sourceTypeId"] == "nsu=http://example.com/custom;i=3001"
    )
    assert synthetic["elementId"].startswith("urn:") and ":objecttype:" in synthetic["elementId"]
    assert synthetic["namespaceUri"] == "http://example.com/custom"
    assert synthetic["displayName"] == "FakeMachineConfig"
    assert synthetic["schema"]["x-opcua-structureTypeId"] == "nsu=http://example.com/custom;i=3001"
    assert synthetic["schema"]["x-opcua-nodeId"] == "nsu=http://example.com/custom;i=3001"
    synthetic_ref = synthetic["schema"]["oneOf"][1]["$ref"]
    assert isinstance(synthetic_ref, str) and synthetic_ref.startswith("#/$defs/")
    synthetic_def = synthetic["schema"]["$defs"][synthetic_ref.split("#/$defs/", 1)[1]]
    assert synthetic_def["type"] == "object"
    synthetic_thresholds_schema = synthetic_def["properties"]["thresholds"]
    assert isinstance(synthetic_thresholds_schema.get("$ref"), str)
    synthetic_thresholds_ref = synthetic_thresholds_schema["$ref"]
    assert synthetic_thresholds_ref.startswith("#/$defs/")
    synthetic_thresholds_def_key = synthetic_thresholds_ref.split("#/$defs/", 1)[1]
    assert synthetic["schema"]["$defs"][synthetic_thresholds_def_key]["properties"]["min"]["type"] == "number"
    assert synthetic["schema"]["$defs"][synthetic_thresholds_def_key]["properties"]["max"]["type"] == "number"

    second = payload["result"][1]
    assert second["schema"]["x-opcua-references"][0]["referenceType"] == "HasSubtype"
    assert second["schema"]["x-opcua-references"][0]["targetNodeId"] == "nsu=http://example.com/custom;i=1001"
    assert second["schema"]["x-opcua-superTypeNodeId"] == "nsu=http://example.com/custom;i=1001"
    assert [item["elementId"] for item in second["related"]["instances"]] == ["sensor-root"]


def test_v1_objecttypes_can_disable_opcua_schema_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("I3X_ENABLE_MCP", "1")
    monkeypatch.setenv("I3X_SKIP_OPCUA_CONNECT", "1")
    monkeypatch.setenv("I3X_ENABLE_WRITES", "0")
    monkeypatch.setenv("I3X_MCP_INCLUDE_OPCUA_METADATA", "0")

    app = create_app()
    with TestClient(app) as test_client:
        _configure_test_app(app)
        response = test_client.get("/v1/objecttypes")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["result"]
    assert all(not _contains_opcua_extension_key(item["schema"]) for item in payload["result"])


def test_v1_objects_list_include_metadata_asset_type_namespace_uses_type_source_id(client: TestClient) -> None:
    response = client.post(
        "/v1/objects/list",
        json={"elementIds": ["asset-root"], "includeMetadata": True},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True

    result = payload["results"][0]["result"]
    assert result["typeElementId"].startswith("urn:") and ":objecttype:" in result["typeElementId"]

    metadata = result["metadata"]
    assert metadata["sourceTypeId"] == "nsu=http://example.com/custom;i=1001"
    assert metadata["typeNamespaceUri"] == "http://example.com/custom"


def test_v1_history_query_ignores_unsupported_component_history_reads(client: TestClient) -> None:
    app = _fastapi_app(client)

    async def read_history_values(
        node_ids: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> dict[str, list[Any]]:
        del start_time, end_time
        return {node_id: [] for node_id in node_ids}

    app.state.opcua_client.read_history_values = read_history_values
    response = client.post(
        "/v1/objects/history",
        json={
            "elementIds": ["asset-root"],
            "startTime": "2026-01-01T00:00:00Z",
            "endTime": "2026-01-02T00:00:00Z",
            "maxDepth": 2,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    result = payload["results"][0]["result"]
    assert result["isComposition"] is True
    assert "components" in result


def test_v1_value_query_structured_null_body_normalized_to_goodnodata(client: TestClient) -> None:
    async def read_data_values(node_ids: list[str]) -> list[Any]:
        assert node_ids == ["ns=2;s=Temperature"]
        return [
            SimpleNamespace(
                Value=SimpleNamespace(Value=FakeExtensionObject("ns=1;i=3001", None)),
                StatusCode=SimpleNamespace(name="Good", is_good=lambda: True),
                SourceTimestamp=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                ServerTimestamp=None,
            )
        ]

    _fastapi_app(client).state.opcua_client.read_data_values = read_data_values

    response = client.post(
        "/v1/objects/value",
        json={
            "elementIds": ["property-abc"],
            "maxDepth": 1,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    result = payload["results"][0]["result"]
    assert result["value"] is None
    assert result["quality"] == "GoodNoData"


def test_v1_bulk_update_values_success_when_enabled(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")

    response = client.put(
        "/v1/objects/value",
        json={
            "updates": [
                {
                    "elementId": "property-abc",
                    "value": {
                        "value": 55.25,
                        "quality": "Good",
                        "timestamp": "2026-01-01T00:00:00Z",
                    },
                }
            ]
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["results"][0]["success"] is True
    assert payload["results"][0]["result"] is None
    assert _fastapi_app(client).state.opcua_client.values["ns=2;s=Temperature"] == 55.25
    quality, timestamp = _fastapi_app(client).state.opcua_client.last_write_vqt_by_node_id["ns=2;s=Temperature"]
    assert quality == "Good"
    assert timestamp == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_v1_bulk_update_values_supports_partial_failure(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")

    response = client.put(
        "/v1/objects/value",
        json={
            "updates": [
                {
                    "elementId": "property-abc",
                    "value": {
                        "value": 77.0,
                        "quality": "Good",
                        "timestamp": "2026-01-01T00:00:00Z",
                    },
                },
                {
                    "elementId": "does-not-exist",
                    "value": {
                        "value": 1,
                        "quality": "Good",
                        "timestamp": "2026-01-01T00:00:00Z",
                    },
                },
            ]
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["results"][0]["success"] is True
    assert payload["results"][0]["elementId"] == "property-abc"

    failing = payload["results"][1]
    assert failing["success"] is False
    assert failing["elementId"] == "does-not-exist"
    assert failing["error"]["code"] == 404
    assert failing["responseDetail"]["status"] == 404

    assert _fastapi_app(client).state.opcua_client.values["ns=2;s=Temperature"] == 77.0


def test_v1_bulk_update_values_allows_noop_when_target_not_writable(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")
    app = _fastapi_app(client)
    app.state.opcua_client.writable_by_node_id["ns=2;s=Temperature"] = False

    async def read_data_values_same_value(node_ids: list[str]) -> list[Any]:
        assert node_ids == ["ns=2;s=Temperature"]
        return [
            SimpleNamespace(
                Value=SimpleNamespace(Value=42.5),
                StatusCode=SimpleNamespace(name="Good", is_good=lambda: True),
                SourceTimestamp=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                ServerTimestamp=None,
            )
        ]

    app.state.opcua_client.read_data_values = read_data_values_same_value

    response = client.put(
        "/v1/objects/value",
        json={
            "updates": [
                {
                    "elementId": "property-abc",
                    "value": {
                        "value": 42.5,
                        "quality": "Good",
                        "timestamp": "2026-01-01T12:00:00Z",
                    },
                }
            ]
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["results"][0]["success"] is True


def test_v1_subscription_register_missing_client_id_returns_400(client: TestClient) -> None:
    response = client.post(
        "/v1/subscriptions/register",
        json={
            "subscriptionId": "sub-1",
            "elementIds": ["property-abc"],
            "maxDepth": 1,
        },
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == 400


def test_v1_subscription_sync_missing_client_id_returns_400(client: TestClient) -> None:
    response = client.post(
        "/v1/subscriptions/sync",
        json={
            "subscriptionId": "sub-1",
            "acknowledgeSequence": 0,
        },
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == 400


def test_v1_subscription_register_missing_subscription_returns_404(client: TestClient) -> None:
    response = client.post(
        "/v1/subscriptions/register",
        json={
            "clientId": "my-app-instance-001",
            "subscriptionId": "missing",
            "elementIds": ["property-abc"],
            "maxDepth": 1,
        },
    )
    assert response.status_code == 404
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == 404


def test_v1_subscription_unregister_missing_client_id_returns_400(client: TestClient) -> None:
    response = client.post(
        "/v1/subscriptions/unregister",
        json={
            "subscriptionId": "sub-1",
            "elementIds": ["property-abc"],
            "maxDepth": 1,
        },
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == 400


def test_v1_subscription_unregister_missing_subscription_returns_404(client: TestClient) -> None:
    response = client.post(
        "/v1/subscriptions/unregister",
        json={
            "clientId": "my-app-instance-001",
            "subscriptionId": "missing",
            "elementIds": ["property-abc"],
            "maxDepth": 1,
        },
    )
    assert response.status_code == 404
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == 404


def test_openapi_operations_are_grouped_by_workflow(client: TestClient) -> None:
    generated = client.get("/openapi.json").json()
    assert [tag["name"] for tag in generated["tags"]] == [
        "Info",
        "Exploratory",
        "Query",
        "Update",
        "Subscribe",
        "Diagnostics",
        "MCP Protocol",
        "MCP Helpers",
    ]
    for path, methods in generated["paths"].items():
        for method, operation in methods.items():
            if method not in {"get", "post", "put", "delete", "patch", "options", "head"}:
                continue
            if path == "/v1/info":
                expected_tag = "Info"
            elif path == "/mcp":
                expected_tag = "MCP Protocol"
            elif path.startswith("/mcp/"):
                expected_tag = "MCP Helpers"
            elif path.startswith("/ua") or path.startswith("/model/"):
                expected_tag = "Diagnostics"
            elif path.startswith("/v1/subscriptions"):
                expected_tag = "Subscribe"
            elif path in {"/v1/objects/value", "/v1/objects/history"}:
                expected_tag = "Update" if method == "put" else "Query"
            else:
                expected_tag = "Exploratory"
            assert operation["tags"] == [expected_tag], (method, path)
    assert generated["paths"]["/v1/objects/value"]["post"]["operationId"] == "queryLastKnownValuesV1"
    assert generated["paths"]["/v1/namespaces"]["get"]["operationId"] == "getNamespacesV1"


def test_openapi_errors_match_runtime(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    invalid_limit = client.get("/model/namespace-gaps", params={"limit": 0})
    assert invalid_limit.status_code == 400
    assert_documented_response(client, "get", "/model/namespace-gaps", 400, invalid_limit.json())

    validation = client.post("/v1/objects/value", json={"maxDepth": 1})
    assert validation.status_code == 400
    assert_documented_response(client, "post", "/v1/objects/value", 400, validation.json())

    missing = client.post("/v1/subscriptions/sync", json={"clientId": "example-client", "subscriptionId": "missing"})
    assert missing.status_code == 404
    assert_documented_response(client, "post", "/v1/subscriptions/sync", 404, missing.json())

    body = {
        "updates": [
            {
                "elementId": "property-abc",
                "value": {
                    "value": 23.5,
                    "quality": "Good",
                    "timestamp": "2026-10-14T06:00:00Z",
                },
            }
        ]
    }
    for path in ("/v1/objects/value", "/v1/objects/history"):
        disabled = client.put(path, json=body)
        assert disabled.status_code == 501
        assert_documented_response(client, "put", path, 501, disabled.json())

    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")
    _fastapi_app(client).state.opcua_client.history_update_support = False
    unsupported = client.put("/v1/objects/history", json=body)
    assert unsupported.status_code == 501
    assert "results" in unsupported.json()
    assert_documented_response(client, "put", "/v1/objects/history", 501, unsupported.json())


def test_openapi_validation_and_streaming_contracts(client: TestClient) -> None:
    document = client.get("/openapi.json").json()
    for methods in document["paths"].values():
        for operation in methods.values():
            if "responses" not in operation:
                continue
            assert "422" not in operation["responses"]
            assert "400" in operation["responses"]
            assert "500" in operation["responses"]
    for path, method in (("/v1/subscriptions/stream", "post"),):
        content = document["paths"][path][method]["responses"]["200"]["content"]
        assert set(content) == {"text/event-stream"}
        assert content["text/event-stream"]["schema"] == {"type": "string"}
        assert "\n\n" in content["text/event-stream"]["example"]
    sync = document["paths"]["/v1/subscriptions/sync"]["post"]["responses"]
    assert sync["200"]["content"]["application/json"]["schema"]["$ref"]
    partial_schema = document["components"]["schemas"]["PartialSyncResponse"]
    assert set(partial_schema["required"]) == {"success", "result", "responseDetail"}
    assert document["paths"]["/mcp"]["post"]["responses"]["202"]["content"] == {}
    assert document["paths"]["/mcp"]["get"]["responses"]["200"]["content"]["text/event-stream"]["schema"] == {
        "type": "string"
    }
    assert "securitySchemes" not in document["components"]
    assert document["info"]["contact"]["url"].startswith("https://")
    assert document["externalDocs"]["url"].startswith("https://")
    assert "304" not in document["paths"]["/model/metrics"]["get"]["responses"]


def test_openapi_mcp_jsonrpc_responses_match_runtime(client: TestClient) -> None:
    for message in (
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 2, "method": "unknown-method"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
    ):
        response = client.post("/mcp", json=message)
        assert response.status_code == 200
        assert_documented_response(client, "post", "/mcp", 200, response.json())
    notification = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert notification.status_code == 202
    assert notification.content == b""


@pytest.mark.parametrize(
    "path",
    [
        "/v1/objects",
        "/v1/objecttypes",
        "/v1/namespaces",
        "/v1/relationshiptypes",
        "/model/namespace-gaps",
    ],
)
def test_openapi_conditional_get_matches_runtime(client: TestClient, path: str) -> None:
    document = client.get("/openapi.json").json()
    operation = document["paths"][path]["get"]
    parameter = next(item for item in operation["parameters"] if item["name"] == "If-None-Match")
    assert parameter["in"] == "header"
    assert parameter["required"] is False
    assert "content" not in operation["responses"]["304"]
    for status in ("200", "304"):
        assert {"ETag", "I3X-Model-Revision"} <= set(operation["responses"][status]["headers"])
    response = client.get(path)
    assert response.status_code == 200
    assert_documented_response(client, "get", path, 200, response.json())
    conditional = client.get(path, headers={"If-None-Match": response.headers["etag"]})
    assert conditional.status_code == 304
    assert conditional.content == b""
    assert conditional.headers["etag"] == response.headers["etag"]
    tools = client.get("/mcp/tools").json()["tools"]
    excluded_tools = load_overrides().get("excluded_tools", [])
    excluded_tools = (
        {name for name in excluded_tools if isinstance(name, str)} if isinstance(excluded_tools, list) else set()
    )
    if operation["operationId"] in excluded_tools:
        assert operation["operationId"] not in tools
        return
    assert "If-None-Match" not in tools[operation["operationId"]]["inputSchema"]["properties"]


def test_openapi_examples_match_schemas(client: TestClient) -> None:
    document = client.get("/openapi.json").json()

    def validate_example(schema: dict[str, Any], example: Any) -> None:
        Draft202012Validator(
            {**schema, "components": document["components"]},
            format_checker=Draft202012Validator.FORMAT_CHECKER,
        ).validate(example)

    schemas = document["components"]["schemas"]
    for schema in schemas.values():
        for example in schema.get("examples", []):
            validate_example(schema, example)
    for methods in document["paths"].values():
        for operation in methods.values():
            containers = [operation.get("requestBody", {}), *operation.get("responses", {}).values()]
            for container in containers:
                for content in container.get("content", {}).values():
                    if "example" in content:
                        validate_example(content["schema"], content["example"])
                    for example in content.get("examples", {}).values():
                        validate_example(content["schema"], example["value"])


def test_i3x_openapi_contract_marks_required_fields(client: TestClient) -> None:
    document = client.get("/openapi.json").json()
    schemas = document["components"]["schemas"]

    assert {"success", "result"} <= set(schemas["SuccessResponse_ServerInfo_"]["required"])
    assert "schema" in schemas["ObjectTypeResponse"]["required"]
    assert "values" in schemas["HistoricalValueResult"]["required"]

    register_schema = schemas["RegisterMonitoredItemsRequest"]
    assert {"clientId", "subscriptionId", "elementIds"} <= set(register_schema["required"])
    assert register_schema["properties"]["maxDepth"]["default"] == 1

    unregister_schema = schemas["UnregisterMonitoredItemsRequest"]
    assert {"clientId", "subscriptionId", "elementIds"} <= set(unregister_schema["required"])
    assert "maxDepth" not in unregister_schema["properties"]

    sync_schema = schemas["SyncRequest"]
    assert {"clientId", "subscriptionId"} <= set(sync_schema["required"])
    assert "lastSequenceNumber" in sync_schema["properties"]
    assert "acknowledgeSequence" not in sync_schema["properties"]

    stream_schema = schemas["StreamRequest"]
    assert {"clientId", "subscriptionId"} <= set(stream_schema["required"])
    assert "acknowledgeSequence" not in stream_schema["properties"]


def _resolve_openapi_schema(schema: Any, components: Mapping[str, Any]) -> Any:
    if not isinstance(schema, Mapping):
        return schema

    if "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str):
            return dict(schema)
        prefix = "#/components/schemas/"
        if not ref.startswith(prefix):
            return {"$ref": ref}
        schema_name = ref.removeprefix(prefix)
        schemas = components.get("schemas", {})
        if isinstance(schemas, Mapping) and schema_name in schemas:
            return _resolve_openapi_schema(schemas[schema_name], components)
        return {"$ref": ref}

    resolved: dict[str, Any] = dict(schema)
    for key in ("allOf", "anyOf", "oneOf"):
        value = resolved.get(key)
        if isinstance(value, list):
            resolved[key] = [_resolve_openapi_schema(item, components) for item in value]

    properties = resolved.get("properties")
    if isinstance(properties, Mapping):
        resolved["properties"] = {
            str(name): _resolve_openapi_schema(value, components) for name, value in properties.items()
        }

    items = resolved.get("items")
    if items is not None:
        resolved["items"] = _resolve_openapi_schema(items, components)

    return resolved


def _pick_concrete_schema(schema: Any) -> Any:
    if not isinstance(schema, Mapping):
        return schema

    for key in ("anyOf", "oneOf"):
        variants = schema.get(key)
        if isinstance(variants, list) and variants:
            for variant in variants:
                if isinstance(variant, Mapping) and variant.get("type") == "null":
                    continue
                return _pick_concrete_schema(variant)
            return _pick_concrete_schema(variants[0])

    return schema


def _sample_from_schema(schema: Any, *, property_name: str = "") -> Any:
    concrete = _pick_concrete_schema(schema)
    if not isinstance(concrete, Mapping):
        return "sample"

    enum_values = concrete.get("enum")
    if isinstance(enum_values, list) and enum_values:
        return enum_values[0]

    schema_type = concrete.get("type")
    if isinstance(schema_type, list):
        non_null_types = [item for item in schema_type if item != "null"]
        schema_type = non_null_types[0] if non_null_types else schema_type[0]

    if schema_type == "string":
        lowered = property_name.lower()
        if "time" in lowered:
            return "2026-01-01T00:00:00Z"
        if lowered.endswith("id") or lowered.endswith("ids"):
            return "property-abc"
        return "sample"

    if schema_type == "integer":
        minimum = concrete.get("minimum")
        if isinstance(minimum, int):
            return minimum
        return 1

    if schema_type == "number":
        minimum = concrete.get("minimum")
        if isinstance(minimum, (int, float)):
            return float(minimum)
        return 1.0

    if schema_type == "boolean":
        return False

    if schema_type == "array":
        item_schema = concrete.get("items", {"type": "string"})
        return [_sample_from_schema(item_schema, property_name=property_name.removesuffix("s"))]

    properties = concrete.get("properties")
    if schema_type == "object" or isinstance(properties, Mapping):
        if not isinstance(properties, Mapping):
            return {}
        required = concrete.get("required", [])
        if not isinstance(required, list):
            required = []
        result: dict[str, Any] = {}
        for key in required:
            if not isinstance(key, str):
                continue
            result[key] = _sample_from_schema(properties.get(key, {}), property_name=key)
        return result

    return {}


def _build_required_mcp_arguments(input_schema: Mapping[str, Any]) -> dict[str, Any]:
    properties = input_schema.get("properties", {})
    required = input_schema.get("required", [])
    if not isinstance(properties, Mapping) or not isinstance(required, list):
        return {}

    args: dict[str, Any] = {}
    for name in required:
        if not isinstance(name, str):
            continue
        args[name] = _sample_from_schema(properties.get(name, {}), property_name=name)
    return args


def _with_runtime_argument_overrides(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    subscription_id: str | None,
) -> dict[str, Any]:
    result = dict(arguments)

    if tool_name == "readContainerValues":
        result["containerId"] = "asset-root"
    if tool_name == "resolveBrowsePath":
        result["path"] = "Machine"
    if tool_name == "getCompositionProperties":
        result["elementIds"] = ["asset-root"]
    if "elementId" in result:
        result["elementId"] = "property-abc"
    if "element_id" in result:
        result["element_id"] = "property-abc"

    body = result.get("body")
    if isinstance(body, Mapping):
        body_dict = dict(body)
        if "elementIds" in body_dict:
            body_dict["elementIds"] = ["property-abc"]
        if "startTime" in body_dict:
            body_dict.setdefault("startTime", "2026-01-01T00:00:00Z")
        if "endTime" in body_dict:
            body_dict.setdefault("endTime", "2026-01-02T00:00:00Z")
        if "maxDepth" in body_dict:
            body_dict.setdefault("maxDepth", 1)
        if tool_name == "createSubscription":
            body_dict.setdefault("clientId", "mcp-runtime-smoke")
            body_dict.setdefault("displayName", "MCP Runtime Smoke")
        if subscription_id is not None:
            if "subscriptionId" in body_dict:
                body_dict["subscriptionId"] = subscription_id
            if "subscriptionIds" in body_dict:
                body_dict["subscriptionIds"] = [subscription_id]
        result["body"] = body_dict

    return result


def _operation_id_for(client: TestClient, method: str, path: str) -> str:
    openapi = client.get("/openapi.json").json()
    paths = openapi.get("paths", {})
    assert isinstance(paths, Mapping)
    methods = paths.get(path, {})
    assert isinstance(methods, Mapping), f"Path not found in OpenAPI: {path}"
    details = methods.get(method.lower(), {})
    assert isinstance(details, Mapping), f"Method not found in OpenAPI: {method} {path}"
    operation_id = details.get("operationId")
    assert isinstance(operation_id, str) and operation_id
    return operation_id


def _configured_excluded_tools() -> set[str]:
    excluded = load_overrides().get("excluded_tools", [])
    return {name for name in excluded if isinstance(name, str)} if isinstance(excluded, list) else set()


def test_mcp_write_tools_hidden(client: TestClient) -> None:
    tools = client.get("/mcp/tools").json()["tools"]
    update_value_id = _operation_id_for(client, "PUT", "/v1/objects/value")
    assert update_value_id not in tools


def test_path_style_value_write_route_is_not_registered(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/v1/objects/{element_id}/value" not in paths
    assert client.put("/v1/objects/property-abc/value", json={"value": 19.5}).status_code == 404


def test_path_style_history_routes_are_not_registered(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/v1/objects/{element_id}/history" not in paths
    assert client.get("/v1/objects/property-abc/history").status_code == 404
    assert client.put("/v1/objects/property-abc/history", json={}).status_code == 404


def test_mcp_tool_overrides_match_live_tools(client: TestClient) -> None:
    overrides = load_tool_overrides()
    tools = client.get("/mcp/tools").json()["tools"]
    excluded_tools = _configured_excluded_tools()

    active_overrides = set(overrides) - excluded_tools
    unknown_overrides = sorted(active_overrides - set(tools))
    assert unknown_overrides == []

    for tool_name, override in overrides.items():
        if tool_name in excluded_tools:
            assert tool_name not in tools
            continue
        tool = tools[tool_name]
        assert tool["description"] == override["description"]
        assert tool["priority"] == override.get("priority", "normal")
        assert tool["keywords"] == override.get("keywords", [])


def test_mcp_model_overview_and_root_resources_read_rest(client: TestClient) -> None:
    overview_response = client.post("/mcp/resources/read", json={"uri": "i3x://model/overview"})
    assert overview_response.status_code == 200
    overview_content = overview_response.json()["contents"][0]
    assert overview_content["mimeType"] == "application/json"
    overview = json.loads(overview_content["text"])
    assert overview["modelAvailable"] is True
    assert overview["summary"]["rootCount"] > 0
    assert any(root["uri"] == "i3x://roots/asset-root" for root in overview["roots"])

    root_response = client.post("/mcp/resources/read", json={"uri": "i3x://roots/asset-root"})
    assert root_response.status_code == 200
    root_content = root_response.json()["contents"][0]
    assert root_content["mimeType"] == "application/json"
    root = json.loads(root_content["text"])
    assert root["elementId"] == "asset-root"
    assert root["displayName"]


# Bulk batch limits: every bulk array is capped at ``MAX_BULK_ITEMS`` so one request cannot turn
# into thousands of upstream OPC UA round-trips (REST parity with the MCP batch limit).
_BULK_BATCH_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("post", "/v1/objecttypes/query", "elementIds"),
    ("post", "/v1/relationshiptypes/query", "elementIds"),
    ("post", "/v1/objects/list", "elementIds"),
    ("post", "/v1/objects/related", "elementIds"),
    ("post", "/v1/objects/value", "elementIds"),
    ("post", "/v1/objects/history", "elementIds"),
    ("put", "/v1/objects/value", "updates"),
    ("put", "/v1/objects/history", "updates"),
    ("post", "/v1/subscriptions/register", "elementIds"),
    ("post", "/v1/subscriptions/unregister", "elementIds"),
    ("post", "/v1/subscriptions/list", "subscriptionIds"),
    ("post", "/v1/subscriptions/delete", "subscriptionIds"),
)


def _batch_item(field: str, index: int) -> dict[str, Any] | str:
    if field == "updates":
        return {
            "elementId": f"property-{index}",
            "value": {
                "value": 1.0,
                "quality": "Good",
                "timestamp": "2026-01-01T00:00:00Z",
            },
        }
    return f"batch-element-{index}"


def _batch_body(method: str, path: str, field: str, count: int) -> dict[str, Any]:
    body: dict[str, Any] = {field: [_batch_item(field, index) for index in range(count)]}
    if method == "post" and path == "/v1/objects/history":
        body["startTime"] = "2026-01-01T00:00:00Z"
        body["endTime"] = "2026-01-02T00:00:00Z"
    if path in {"/v1/subscriptions/register", "/v1/subscriptions/unregister"}:
        body["clientId"] = "batch-client"
        body["subscriptionId"] = "missing-subscription"
    if path in {"/v1/subscriptions/list", "/v1/subscriptions/delete"}:
        body["clientId"] = "batch-client"
    return body


def _request_body_schema(document: dict[str, Any], method: str, path: str) -> dict[str, Any]:
    body_schema: dict[str, Any] = document["paths"][path][method]["requestBody"]["content"]["application/json"][
        "schema"
    ]
    ref = body_schema.get("$ref")
    if isinstance(ref, str):
        components: dict[str, Any] = document["components"]["schemas"]
        return cast(dict[str, Any], components[ref.split("/")[-1]])
    return body_schema


@pytest.mark.parametrize(("method", "path", "field"), _BULK_BATCH_FIELDS)
def test_bulk_batch_above_the_limit_is_rejected_with_the_documented_400(
    client: TestClient,
    method: str,
    path: str,
    field: str,
) -> None:
    response = client.request(
        method.upper(),
        path,
        json=_batch_body(method, path, field, MAX_BULK_ITEMS + 1),
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == 400
    assert_documented_response(client, method, path, 400, payload)


@pytest.mark.parametrize(("method", "path", "field"), _BULK_BATCH_FIELDS)
def test_bulk_batch_at_the_limit_passes_contract_validation(
    client: TestClient,
    method: str,
    path: str,
    field: str,
) -> None:
    # Reaching the cap must not be a validation failure - only exceeding it is.
    response = client.request(
        method.upper(),
        path,
        json=_batch_body(method, path, field, MAX_BULK_ITEMS),
    )
    assert response.status_code != 400


def test_openapi_advertises_the_bulk_batch_limit(client: TestClient) -> None:
    document = client.get("/openapi.json").json()
    for method, path, field in _BULK_BATCH_FIELDS:
        properties = _request_body_schema(document, method, path)["properties"]
        assert properties[field].get("maxItems") == MAX_BULK_ITEMS, (path, field)
