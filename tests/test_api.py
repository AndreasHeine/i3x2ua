from __future__ import annotations

import base64
import os
import time
from collections.abc import Generator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest
from asyncua import ua
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from i3x_server.api.v1.monolithic import _expanded_node_id, _to_json_safe_value
from i3x_server.bootstrap.app_factory import create_app
from i3x_server.infrastructure.opcua.client import (
    OpcUaConnectionSnapshot,
    OpcUaNamespaceInfo,
    OpcUaOperationalLimits,
    OpcUaRequestMetrics,
    OpcUaSubscriptionCapabilities,
)
from i3x_server.infrastructure.subscriptions.service import SubscriptionService
from i3x_server.mcp import _safe_internal_request_url, get_api_prefix, load_tool_overrides
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


def _csp_source_tokens(csp: str) -> set[str]:
    tokens: set[str] = set()
    for directive in csp.split(";"):
        parts = directive.strip().split()
        if len(parts) > 1:
            tokens.update(parts[1:])
    return tokens


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
        self.last_write_variant_type_by_node_id: dict[str, str | None] = {}
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

    async def write_value(self, node_id: str, value: Any, variant_type: str | None = None) -> None:
        failure = self.write_failures.get(node_id)
        if failure is not None:
            self._request_metrics.failed_request_count += 1
            raise failure
        self.last_write_variant_type_by_node_id[node_id] = variant_type
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
    os.environ["I3X_ENABLE_MCP"] = "1"
    os.environ["I3X_SKIP_OPCUA_CONNECT"] = "1"
    os.environ["I3X_ENABLE_WRITES"] = "0"
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


@pytest.fixture
def client_without_mcp() -> Generator[TestClient, None, None]:
    previous_enable_mcp = os.environ.get("I3X_ENABLE_MCP")
    previous_skip_connect = os.environ.get("I3X_SKIP_OPCUA_CONNECT")
    previous_enable_writes = os.environ.get("I3X_ENABLE_WRITES")
    os.environ.pop("I3X_ENABLE_MCP", None)
    os.environ["I3X_SKIP_OPCUA_CONNECT"] = "1"
    os.environ["I3X_ENABLE_WRITES"] = "0"
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


def test_get_model(client: TestClient) -> None:
    response = client.get("/model")
    assert response.status_code == 404


def test_get_data_value(client: TestClient) -> None:
    response = client.get("/data/property-abc")
    assert response.status_code == 404


def test_invoke_action(client: TestClient) -> None:
    response = client.post("/action/action-def/invoke", json={"args": [1, "x"]})
    assert response.status_code in {404, 405}


def test_v1_info(client: TestClient) -> None:
    response = client.get("/v1/info")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["result"]["specVersion"] == "1.0"
    assert payload["result"]["capabilities"]["query"]["history"] is True
    assert payload["result"]["capabilities"]["update"]["current"] is False
    assert payload["result"]["capabilities"]["subscribe"]["stream"] is True


def test_v1_info_write_capability_enabled_with_flag(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")

    response = client.get("/v1/info")

    assert response.status_code == 200
    payload = response.json()
    assert payload["result"]["capabilities"]["update"]["current"] is True


def test_landing_page_with_mcp_enabled(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    text = response.text
    assert "/static/logo-small.png" in text
    assert "i3X API Gateway for OPC UA" in text
    assert "Turn any OPC UA server into" in text
    assert 'href="/docs"' in text
    assert 'href="/view?endpoint=/v1/info' in text
    assert 'href="/view?endpoint=/ua/status' in text
    assert 'href="/view?endpoint=/ua/connection' in text
    assert 'href="/view?endpoint=/ua/limits' in text
    assert 'href="/view?endpoint=/ua/metrics' in text


def test_landing_page_with_mcp_disabled(client_without_mcp: TestClient) -> None:
    response = client_without_mcp.get("/")
    assert response.status_code == 200
    text = response.text
    assert 'href="/docs"' in text
    assert 'href="/view?endpoint=/v1/info' in text
    assert 'href="/view?endpoint=/ua/status' in text
    assert 'href="/view?endpoint=/ua/connection' in text
    assert 'href="/view?endpoint=/ua/limits' in text
    assert 'href="/view?endpoint=/ua/metrics' in text


def test_docs_csp_allows_swagger_cdn_assets(client: TestClient) -> None:
    response = client.get("/docs")
    assert response.status_code == 200
    csp_sources = _csp_source_tokens(response.headers.get("Content-Security-Policy", ""))
    assert "https://cdn.jsdelivr.net" in csp_sources
    assert "https://fastapi.tiangolo.com" in csp_sources


def test_landing_page_csp_remains_strict(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    csp_sources = _csp_source_tokens(response.headers.get("Content-Security-Policy", ""))
    assert "https://cdn.jsdelivr.net" not in csp_sources
    assert "https://fastapi.tiangolo.com" not in csp_sources
    assert "'unsafe-inline'" not in csp_sources


def test_static_logo_is_served(client: TestClient) -> None:
    response = client.get("/static/logo-small.png")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")


def test_api_viewer_page(client: TestClient) -> None:
    response = client.get("/view?endpoint=/v1/info&label=Server%20Info")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Loading..." in response.text
    assert "Back" in response.text
    assert "/static/logo-small.png" in response.text
    assert "i3X API Gateway for OPC UA" in response.text


def test_api_viewer_escapes_query_inputs(client: TestClient) -> None:
    response = client.get('/view?endpoint=";alert(1);//&label=%3Cscript%3Ealert(1)%3C/script%3E')
    assert response.status_code == 200
    text = response.text

    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" not in text
    assert '";alert(1);//' not in text


def test_mcp_tools_viewer_page(client: TestClient) -> None:
    response = client.get("/mcp-tools-viewer")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "MCP Tools" in response.text
    assert "Back" in response.text
    assert "/static/logo-small.png" in response.text
    assert "i3X API Gateway for OPC UA" in response.text


def test_ua_state(client: TestClient) -> None:
    response = client.get("/ua/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    result = payload["result"]
    assert "nodeId" not in result
    assert "hasTypeDefinition" not in result
    assert "quality" not in result
    assert "timestamp" not in result
    assert result["State"] == "Running"
    assert result["BuildInfo"]["ProductName"] == "UA Server"


def test_ua_connection(client: TestClient) -> None:
    response = client.get("/ua/connection")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    result = payload["result"]
    assert result["state"] == "Connected"
    assert result["endpoint"] == "opc.tcp://opcua.umati.app:4843"
    assert result["since"].endswith("Z")


def test_ua_limits(client: TestClient) -> None:
    response = client.get("/ua/limits")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    result = payload["result"]
    assert result["operationalLimits"]["maxNodesPerBrowse"] == 1000
    assert result["operationalLimits"]["maxNodesPerRead"] == 1000
    assert result["serverCapabilities"]["maxMonitoredItemsPerCall"] == 1
    assert result["serverCapabilities"]["maxSubscriptions"] == 100


def test_ua_metrics(client: TestClient) -> None:
    response = client.get("/ua/metrics")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    result = payload["result"]
    assert result["goodishQualities"] == ["Good", "Uncertain"]
    assert result["readCount"] == 2451
    assert result["writeCount"] == 0
    assert result["browseCount"] == 189
    assert result["methodCallCount"] == 27
    assert result["historyReadCount"] == 62
    assert result["historyWriteCount"] == 0
    assert result["failedRequestCount"] == 14


def test_ua_state_error_shape(client: TestClient) -> None:
    app = _fastapi_app(client)

    async def _raise_status_error() -> Any:
        raise RuntimeError("boom")

    app.state.opcua_client.read_server_status_data_value = _raise_status_error
    response = client.get("/ua/status")
    assert response.status_code == 502
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == 502
    assert payload["error"]["message"] == "Failed to read OPC UA ServerStatus"
    assert payload["responseDetail"]["status"] == 502
    assert payload["responseDetail"]["detail"] == "Failed to read OPC UA ServerStatus"


def test_v1_namespaces(client: TestClient) -> None:
    response = client.get("/v1/namespaces")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert len(payload["result"]) == 3


def test_v1_objecttypes(client: TestClient) -> None:
    response = client.get("/v1/objecttypes")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert len(payload["result"]) >= 3
    first = payload["result"][0]
    assert first["elementId"].startswith("urn:opcua:objecttype:")
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
    assert synthetic["elementId"].startswith("urn:opcua:objecttype:")
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


def test_v1_objecttypes_namespace_uri_is_declared(client: TestClient) -> None:
    namespaces_response = client.get("/v1/namespaces")
    assert namespaces_response.status_code == 200
    namespaces_payload = namespaces_response.json()
    assert namespaces_payload["success"] is True
    declared_namespaces = {item["uri"] for item in namespaces_payload["result"]}

    object_types_response = client.get("/v1/objecttypes")
    assert object_types_response.status_code == 200
    object_types_payload = object_types_response.json()
    assert object_types_payload["success"] is True

    missing = [item for item in object_types_payload["result"] if item["namespaceUri"] not in declared_namespaces]
    assert missing == []


def test_v1_objecttypes_namespace_filter_is_canonicalized(client: TestClient) -> None:
    response = client.get("/v1/objecttypes", params={"namespaceUri": "HTTP://EXAMPLE.COM/CUSTOM/"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert len(payload["result"]) > 0
    assert all(item["namespaceUri"] == "http://example.com/custom" for item in payload["result"])


def test_v1_objecttypes_query(client: TestClient) -> None:
    list_response = client.get("/v1/objecttypes")
    assert list_response.status_code == 200
    listed = list_response.json()["result"]
    existing_id = listed[0]["elementId"]

    response = client.post("/v1/objecttypes/query", json={"elementIds": [existing_id, "missing-type"]})
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["results"][0]["success"] is True
    assert payload["results"][0]["elementId"] == existing_id
    assert payload["results"][0]["result"]["elementId"] == existing_id
    assert payload["results"][1]["success"] is False
    assert payload["results"][1]["elementId"] == "missing-type"
    assert payload["results"][1]["error"]["code"] == 404


def test_v1_objecttypes_includes_builtin_scalar_datatype_reference(client: TestClient) -> None:
    app = _fastapi_app(client)
    app.state.model_cache.nodes_by_id["property-builtin-12"] = ModelNode(
        id="property-builtin-12",
        name="BuiltinString",
        kind="property",
        type="ns=0;i=12",
        children=[],
        source_node_id="ns=2;s=BuiltinString",
    )
    app.state.model_cache.children_by_id.setdefault("asset-root", []).append("property-builtin-12")
    app.state.model_cache.children_by_id["property-builtin-12"] = []
    app.state.model_cache.property_to_node["property-builtin-12"] = "ns=2;s=BuiltinString"

    response = client.get("/v1/objecttypes")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True

    builtin = next(
        (item for item in payload["result"] if item["sourceTypeId"] == "nsu=http://opcfoundation.org/UA/;i=12"),
        None,
    )
    assert builtin is not None
    assert builtin["elementId"].startswith("urn:opcua:objecttype:")
    assert builtin["displayName"] != "UnknownType"
    assert builtin["schema"]["oneOf"][0]["type"] == "null"
    assert builtin["schema"]["oneOf"][1]["type"] == "string"
    assert builtin["schema"]["oneOf"][2] == {"type": "array", "items": {"type": ["string", "null"]}}


def test_v1_objecttypes_includes_builtin_localizedtext_structured_schema(client: TestClient) -> None:
    app = _fastapi_app(client)
    app.state.model_cache.nodes_by_id["property-builtin-21"] = ModelNode(
        id="property-builtin-21",
        name="BuiltinLocalizedText",
        kind="property",
        type="ns=0;i=21",
        children=[],
        source_node_id="ns=2;s=BuiltinLocalizedText",
    )
    app.state.model_cache.children_by_id.setdefault("asset-root", []).append("property-builtin-21")
    app.state.model_cache.children_by_id["property-builtin-21"] = []
    app.state.model_cache.property_to_node["property-builtin-21"] = "ns=2;s=BuiltinLocalizedText"

    response = client.get("/v1/objecttypes")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True

    localized_text = next(
        (item for item in payload["result"] if item["sourceTypeId"] == "nsu=http://opcfoundation.org/UA/;i=21"),
        None,
    )
    assert localized_text is not None
    assert localized_text["displayName"] == "LocalizedText"
    localized_text_ref = localized_text["schema"]["oneOf"][1]["$ref"]
    assert localized_text_ref.startswith("#/$defs/")
    localized_text_def = localized_text["schema"]["$defs"][localized_text_ref.split("#/$defs/", 1)[1]]
    assert localized_text_def["type"] == "object"
    assert set(localized_text_def["properties"]["Locale"]["type"]) == {"null", "string"}
    assert set(localized_text_def["properties"]["Text"]["type"]) == {"null", "string"}


def test_v1_objecttypes_resolves_standard_structured_datatype(client: TestClient) -> None:
    @dataclass(slots=True)
    class _StdStruct96:
        Name: str | None = None

    ua_any = cast(Any, ua)
    previous_registry = getattr(ua_any, "extension_objects_by_datatype", None)
    ua_any.extension_objects_by_datatype = {"ns=0;i=96": _StdStruct96}

    app = _fastapi_app(client)
    app.state.model_cache.nodes_by_id["property-struct-96"] = ModelNode(
        id="property-struct-96",
        name="Structured96",
        kind="property",
        type="ns=0;i=96",
        children=[],
        source_node_id="ns=2;s=Structured96",
    )
    app.state.model_cache.children_by_id.setdefault("asset-root", []).append("property-struct-96")
    app.state.model_cache.children_by_id["property-struct-96"] = []
    app.state.model_cache.property_to_node["property-struct-96"] = "ns=2;s=Structured96"
    try:
        response = client.get("/v1/objecttypes")
        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True

        resolved = next(
            (item for item in payload["result"] if item["sourceTypeId"] == "nsu=http://opcfoundation.org/UA/;i=96"),
            None,
        )
        assert resolved is not None
        assert resolved["displayName"] != "UnknownType"
        assert resolved["schema"]["oneOf"][0]["type"] == "null"
        scalar_ref = resolved["schema"]["oneOf"][1]["$ref"]
        assert scalar_ref.startswith("#/$defs/")
        assert resolved["schema"]["oneOf"][2]["type"] == "array"
        assert resolved["schema"]["oneOf"][2]["items"]["$ref"] == scalar_ref
        assert resolved["schema"]["$defs"][scalar_ref.split("#/$defs/", 1)[1]]["type"] == "object"
        assert resolved["schema"]["x-opcua-structureDataType"] == "nsu=http://opcfoundation.org/UA/;i=96"
    finally:
        ua_any.extension_objects_by_datatype = previous_registry


def test_v1_objecttypes_registers_source_type_alias_element_id(client: TestClient) -> None:
    app = _fastapi_app(client)
    app.state.model_cache.nodes_by_id["property-struct-alias"] = ModelNode(
        id="property-struct-alias",
        name="ConfigAlias",
        kind="property",
        type="ns=1;i=3001",
        children=[],
        source_node_id="ns=2;s=ConfigAlias",
    )
    app.state.model_cache.children_by_id.setdefault("asset-root", []).append("property-struct-alias")
    app.state.model_cache.children_by_id["property-struct-alias"] = []
    app.state.model_cache.property_to_node["property-struct-alias"] = "ns=2;s=ConfigAlias"

    response = client.get("/v1/objecttypes")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True

    alias = next(
        (item for item in payload["result"] if item["elementId"] == "nsu=http://example.com/custom;i=3001"),
        None,
    )
    assert alias is not None
    assert alias["displayName"] == "FakeMachineConfig"
    assert alias["sourceTypeId"] == "nsu=http://example.com/custom;i=3001"
    assert alias["schema"]["x-opcua-nodeId"] == "nsu=http://example.com/custom;i=3001"


def test_v1_objecttypes_does_not_register_action_source_node_id_as_object_type(client: TestClient) -> None:
    app = _fastapi_app(client)
    app.state.model_cache.nodes_by_id["action-ua-11492"] = ModelNode(
        id="action-ua-11492",
        name="GetMonitoredItems",
        kind="action",
        type=None,
        children=[],
        source_node_id="ns=0;i=11492",
        source_type_id=None,
    )
    app.state.model_cache.children_by_id.setdefault("asset-root", []).append("action-ua-11492")
    app.state.model_cache.children_by_id["action-ua-11492"] = []

    response = client.get("/v1/objecttypes")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True

    unresolved = [
        item for item in payload["result"] if item["sourceTypeId"] == "nsu=http://opcfoundation.org/UA/;i=11492"
    ]
    assert unresolved == []


def test_v1_objecttypes_registers_standard_ua_optionset_datatype_as_known(client: TestClient) -> None:
    app = _fastapi_app(client)
    app.state.model_cache.nodes_by_id["property-ua-95"] = ModelNode(
        id="property-ua-95",
        name="AccessRestriction",
        kind="property",
        type="ns=0;i=95",
        children=[],
        source_node_id="ns=2;s=AccessRestriction",
    )
    app.state.model_cache.children_by_id.setdefault("asset-root", []).append("property-ua-95")
    app.state.model_cache.children_by_id["property-ua-95"] = []
    app.state.model_cache.property_to_node["property-ua-95"] = "ns=2;s=AccessRestriction"

    response = client.get("/v1/objecttypes")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True

    resolved = next(
        (item for item in payload["result"] if item["sourceTypeId"] == "nsu=http://opcfoundation.org/UA/;i=95"),
        None,
    )
    assert resolved is not None
    assert resolved["elementId"].startswith("urn:opcua:objecttype:")
    assert resolved["displayName"] == "AccessRestrictionType"
    assert resolved["schema"]["title"] == "AccessRestrictionType"
    assert resolved["schema"]["oneOf"][0]["type"] == "null"
    assert resolved["schema"]["oneOf"][1]["type"] == "integer"


def test_v1_objecttypes_registers_standard_ua_structured_datatype_as_known(client: TestClient) -> None:
    app = _fastapi_app(client)
    app.state.model_cache.nodes_by_id["property-ua-865"] = ModelNode(
        id="property-ua-865",
        name="SessionDiagnostics",
        kind="property",
        type="ns=0;i=865",
        children=[],
        source_node_id="ns=2;s=SessionDiagnostics",
    )
    app.state.model_cache.children_by_id.setdefault("asset-root", []).append("property-ua-865")
    app.state.model_cache.children_by_id["property-ua-865"] = []
    app.state.model_cache.property_to_node["property-ua-865"] = "ns=2;s=SessionDiagnostics"

    response = client.get("/v1/objecttypes")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True

    resolved = next(
        (item for item in payload["result"] if item["sourceTypeId"] == "nsu=http://opcfoundation.org/UA/;i=865"),
        None,
    )
    assert resolved is not None
    assert resolved["elementId"].startswith("urn:opcua:objecttype:")
    assert resolved["displayName"] == "SessionDiagnosticsDataType"
    assert resolved["schema"]["title"] == "SessionDiagnosticsDataType"
    session_ref = resolved["schema"]["oneOf"][1]["$ref"]
    assert session_ref.startswith("#/$defs/")
    assert resolved["schema"]["$defs"][session_ref.split("#/$defs/", 1)[1]]["type"] == "object"


def test_v1_objecttypes_registers_standard_ua_role_permission_as_known(client: TestClient) -> None:
    app = _fastapi_app(client)
    app.state.model_cache.nodes_by_id["property-ua-96"] = ModelNode(
        id="property-ua-96",
        name="RolePermissions",
        kind="property",
        type="ns=0;i=96",
        children=[],
        source_node_id="ns=2;s=RolePermissions",
    )
    app.state.model_cache.children_by_id.setdefault("asset-root", []).append("property-ua-96")
    app.state.model_cache.children_by_id["property-ua-96"] = []
    app.state.model_cache.property_to_node["property-ua-96"] = "ns=2;s=RolePermissions"

    response = client.get("/v1/objecttypes")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True

    resolved = next(
        (item for item in payload["result"] if item["sourceTypeId"] == "nsu=http://opcfoundation.org/UA/;i=96"),
        None,
    )
    assert resolved is not None
    assert resolved["elementId"].startswith("urn:opcua:objecttype:")
    assert resolved["displayName"] == "RolePermissionType"
    assert resolved["schema"]["title"] == "RolePermissionType"
    role_ref = resolved["schema"]["oneOf"][1]["$ref"]
    assert role_ref.startswith("#/$defs/")
    assert resolved["schema"]["$defs"][role_ref.split("#/$defs/", 1)[1]]["type"] == "object"


def test_v1_objecttypes_registers_generic_custom_nodeid_type_as_known(client: TestClient) -> None:
    app = _fastapi_app(client)
    app.state.model_cache.nodes_by_id["property-custom-inferred"] = ModelNode(
        id="property-custom-inferred",
        name="VendorSpecific",
        kind="property",
        type="nsu=http://example.com/custom;s=Vendor.TypeA",
        children=[],
        source_node_id="ns=2;s=VendorSpecific",
    )
    app.state.model_cache.children_by_id.setdefault("asset-root", []).append("property-custom-inferred")
    app.state.model_cache.children_by_id["property-custom-inferred"] = []
    app.state.model_cache.property_to_node["property-custom-inferred"] = "ns=2;s=VendorSpecific"

    response = client.get("/v1/objecttypes")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True

    resolved = next(
        (item for item in payload["result"] if item["sourceTypeId"] == "nsu=http://example.com/custom;s=Vendor.TypeA"),
        None,
    )
    assert resolved is not None
    assert resolved["elementId"].startswith("urn:opcua:objecttype:")
    assert resolved["displayName"] == "DataType"
    assert isinstance(resolved["schema"].get("oneOf"), list)


def test_v1_objecttypes_unresolved_standard_property_datatype_gets_fallback_schema(client: TestClient) -> None:
    app = _fastapi_app(client)
    app.state.model_cache.nodes_by_id["property-ua-14119"] = ModelNode(
        id="property-ua-14119",
        name="OpaqueStandardType",
        kind="property",
        type="ns=0;i=14119",
        children=[],
        source_node_id="ns=2;s=OpaqueStandardType",
    )
    app.state.model_cache.children_by_id.setdefault("asset-root", []).append("property-ua-14119")
    app.state.model_cache.children_by_id["property-ua-14119"] = []
    app.state.model_cache.property_to_node["property-ua-14119"] = "ns=2;s=OpaqueStandardType"

    response = client.get("/v1/objecttypes")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True

    resolved = next(
        (item for item in payload["result"] if item["sourceTypeId"] == "nsu=http://opcfoundation.org/UA/;i=14119"),
        None,
    )
    assert resolved is not None
    assert resolved["elementId"].startswith("urn:opcua:objecttype:")
    assert not resolved["displayName"].startswith("InferredType_")
    assert isinstance(resolved["schema"].get("oneOf"), list)
    assert resolved["schema"]["x-opcua-nodeId"] == "nsu=http://opcfoundation.org/UA/;i=14119"


def test_v1_objecttypes_generic_standard_id_uses_browse_name_lookup(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("i3x_server.api.v1.monolithic._ENABLE_LIVE_TYPE_NAME_LOOKUP", True)
    monkeypatch.setattr("i3x_server.api.v1.monolithic._LIVE_TYPE_NAME_LOOKUP_MAX_PER_REQUEST", 100)

    app = _fastapi_app(client)
    app.state.model_cache.nodes_by_id["property-ua-17364"] = ModelNode(
        id="property-ua-17364",
        name="NetworkAddress",
        kind="property",
        type="ns=0;i=17364",
        children=[],
        source_node_id="ns=2;s=NetworkAddress",
    )
    app.state.model_cache.children_by_id.setdefault("asset-root", []).append("property-ua-17364")
    app.state.model_cache.children_by_id["property-ua-17364"] = []
    app.state.model_cache.property_to_node["property-ua-17364"] = "ns=2;s=NetworkAddress"

    response = client.get("/v1/objecttypes")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True

    resolved = next(
        (item for item in payload["result"] if item["sourceTypeId"] == "nsu=http://opcfoundation.org/UA/;i=17364"),
        None,
    )
    assert resolved is not None
    assert resolved["displayName"] == "PublishSubscribe_SetSecurityKeys"
    assert resolved["schema"]["title"] == "PublishSubscribe_SetSecurityKeys"


def test_v1_objecttypes_does_not_publish_null_opcua_type_id(client: TestClient) -> None:
    app = _fastapi_app(client)
    app.state.model_cache.nodes_by_id["asset-null-type"] = ModelNode(
        id="asset-null-type",
        name="NullTypeAsset",
        kind="asset",
        type=None,
        children=[],
        source_node_id="ns=2;s=NullTypeAsset",
        source_type_id="ns=0;i=0",
    )
    app.state.model_cache.root_ids.append("asset-null-type")
    app.state.model_cache.children_by_id["asset-null-type"] = []
    app.state.model_cache.instances_by_type_id.setdefault("ns=0;i=0", []).append("asset-null-type")

    response = client.get("/v1/objecttypes")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True

    assert all(item["elementId"] != "nsu=http://opcfoundation.org/UA/;i=0" for item in payload["result"])
    assert all(item["sourceTypeId"] != "nsu=http://opcfoundation.org/UA/;i=0" for item in payload["result"])


def test_v1_relationshiptypes(client: TestClient) -> None:
    response = client.get("/v1/relationshiptypes")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert len(payload["result"]) >= 4
    ids = {item["elementId"] for item in payload["result"]}
    assert "HasComponent" in ids
    assert "HasParent" in ids


def test_v1_relationshiptypes_query(client: TestClient) -> None:
    response = client.post(
        "/v1/relationshiptypes/query",
        json={"elementIds": ["HasComponent", "missing-relationship"]},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["results"][0]["success"] is True
    assert payload["results"][0]["result"]["relationshipId"] == "HasComponent"
    assert payload["results"][1]["success"] is False
    assert payload["results"][1]["error"]["code"] == 404


def test_v1_objects_list(client: TestClient) -> None:
    object_types = client.get("/v1/objecttypes").json()["result"]
    expected_type_element_id = object_types[0]["elementId"]

    response = client.post("/v1/objects/list", json={"elementIds": ["asset-root", "missing"]})
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["results"][0]["success"] is True
    assert payload["results"][1]["success"] is False
    first = payload["results"][0]["result"]
    assert first["typeElementId"] == expected_type_element_id


def test_v1_objects_root_uses_hierarchy_only(client: TestClient) -> None:
    response = client.get("/v1/objects", params={"root": "true"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True

    element_ids = {item["elementId"] for item in payload["result"]}
    assert element_ids == {"asset-root", "sensor-root"}


def test_v1_objects_root_excludes_composition_only_children(client: TestClient) -> None:
    app = _fastapi_app(client)
    app.state.model_cache.hierarchy_parent_by_id = {}
    app.state.model_cache.composition_parent_by_id = {
        "sensor-root": "asset-root",
    }

    response = client.get("/v1/objects", params={"root": "true"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True

    element_ids = {item["elementId"] for item in payload["result"]}
    assert element_ids == {"asset-root"}


def test_v1_objects_related_uses_relationships_map_including_graph(client: TestClient) -> None:
    app = _fastapi_app(client)
    app.state.model_cache.relationships_by_id = {
        "asset-root": {
            "HasChildren": ["action-def"],
            "HasComponent": ["property-abc"],
            "Monitors": ["sensor-root"],
        },
        "action-def": {"HasParent": ["asset-root"]},
        "property-abc": {"ComponentOf": ["asset-root"]},
        "sensor-root": {},
    }

    response = client.post(
        "/v1/objects/related",
        json={"elementIds": ["asset-root"], "includeMetadata": False},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True

    related = payload["results"][0]["result"]
    by_relationship = {item["sourceRelationship"]: item["object"]["elementId"] for item in related}
    assert by_relationship["HasChildren"] == "action-def"
    assert by_relationship["HasComponent"] == "property-abc"
    assert by_relationship["Monitors"] == "sensor-root"


def test_v1_objects_related_missing_element_item_includes_response_detail(client: TestClient) -> None:
    response = client.post(
        "/v1/objects/related",
        json={"elementIds": ["asset-root", "does-not-exist"]},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    failing = next(item for item in payload["results"] if not item["success"])
    assert failing["elementId"] == "does-not-exist"
    assert failing["error"]["code"] == 404
    assert failing["responseDetail"]["status"] == 404
    assert failing["responseDetail"]["title"] == "Not Found"
    assert isinstance(failing["responseDetail"]["detail"], str)


def test_v1_objects_list_missing_element_item_includes_response_detail(client: TestClient) -> None:
    response = client.post(
        "/v1/objects/list",
        json={"elementIds": ["asset-root", "does-not-exist"]},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    failing = next(item for item in payload["results"] if not item["success"])
    assert failing["elementId"] == "does-not-exist"
    assert failing["error"]["code"] == 404
    assert failing["responseDetail"]["status"] == 404
    assert failing["responseDetail"]["title"] == "Not Found"


def test_v1_value_missing_element_item_includes_response_detail(client: TestClient) -> None:
    response = client.post(
        "/v1/objects/value",
        json={"elementIds": ["property-abc", "does-not-exist"], "maxDepth": 1},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    failing = next(item for item in payload["results"] if not item["success"])
    assert failing["elementId"] == "does-not-exist"
    assert failing["error"]["code"] == 404
    assert failing["responseDetail"]["status"] == 404
    assert failing["responseDetail"]["title"] == "Not Found"


def test_v1_is_composition_true_when_composition_children_exist(client: TestClient) -> None:
    app = _fastapi_app(client)
    app.state.model_cache.composition_children_by_id = {"asset-root": ["property-abc"]}

    response = client.post(
        "/v1/objects/value",
        json={"elementIds": ["asset-root"], "maxDepth": 1},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["results"][0]["success"] is True
    assert payload["results"][0]["result"]["isComposition"] is True


def test_v1_value_query_container_node_returns_goodnodata_without_direct_read(client: TestClient) -> None:
    app = _fastapi_app(client)
    called = False

    async def read_data_values(node_ids: list[str]) -> list[Any]:
        nonlocal called
        called = True
        assert "ns=2;s=Machine" not in node_ids
        return []

    app.state.opcua_client.read_data_values = read_data_values

    response = client.post(
        "/v1/objects/value",
        json={"elementIds": ["asset-root"], "maxDepth": 1},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert called is False

    result = payload["results"][0]["result"]
    assert result["value"] is None
    assert result["quality"] == "GoodNoData"


def test_v1_is_composition_false_when_no_composition_children(client: TestClient) -> None:
    app = _fastapi_app(client)
    app.state.model_cache.composition_children_by_id = {}

    response = client.post(
        "/v1/objects/value",
        json={"elementIds": ["property-abc"], "maxDepth": 1},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["results"][0]["success"] is True
    assert payload["results"][0]["result"]["isComposition"] is False


def test_v1_value_recursion_uses_composition_not_hierarchy(client: TestClient) -> None:
    app = _fastapi_app(client)
    property_id = "property-abc"
    child_asset_id = "asset-child"
    child_prop_id = "child-prop"

    app.state.model_cache.nodes_by_id[child_asset_id] = ModelNode(
        id=child_asset_id,
        name="ChildAsset",
        kind="asset",
        type=None,
        children=[child_prop_id],
        source_node_id="ns=2;s=ChildAsset",
    )
    app.state.model_cache.nodes_by_id[child_prop_id] = ModelNode(
        id=child_prop_id,
        name="ChildProp",
        kind="property",
        type="ns=1;i=11",
        children=[],
        source_node_id="ns=2;s=ChildProp",
    )
    app.state.model_cache.property_to_node[child_prop_id] = "ns=2;s=ChildProp"
    app.state.model_cache.children_by_id[child_prop_id] = []
    app.state.model_cache.children_by_id[child_asset_id] = [child_prop_id]

    # asset-root has child-asset-id in hierarchy but NOT in composition
    app.state.model_cache.hierarchy_children_by_id["asset-root"] = [property_id, "action-def", child_asset_id]
    app.state.model_cache.composition_children_by_id["asset-root"] = [property_id]
    app.state.model_cache.composition_children_by_id[child_asset_id] = []

    app.state.opcua_client.values["ns=2;s=ChildProp"] = 5.0

    response = client.post(
        "/v1/objects/value",
        json={"elementIds": ["asset-root"], "maxDepth": 5},
    )
    assert response.status_code == 200
    payload = response.json()
    result = payload["results"][0]["result"]
    component_ids = set((result.get("components") or {}).keys())
    assert property_id in component_ids
    assert child_prop_id not in component_ids


def test_v1_objects_related_graph_relationships_appear_in_result(client: TestClient) -> None:
    app = _fastapi_app(client)
    app.state.model_cache.relationships_by_id = {
        "asset-root": {
            "HasChildren": ["action-def"],
            "HasComponent": ["property-abc"],
            "Monitors": ["sensor-root"],
            "ConnectedTo": ["sensor-root"],
        },
        "action-def": {"HasParent": ["asset-root"]},
        "property-abc": {"ComponentOf": ["asset-root"]},
        "sensor-root": {},
    }

    response = client.post(
        "/v1/objects/related",
        json={"elementIds": ["asset-root"]},
    )
    assert response.status_code == 200
    payload = response.json()
    related = payload["results"][0]["result"]
    relationship_types = {item["sourceRelationship"] for item in related}
    assert "Monitors" in relationship_types
    assert "ConnectedTo" in relationship_types


def test_v1_objects_list_include_metadata_uses_expanded_source_type_id(client: TestClient) -> None:
    response = client.post(
        "/v1/objects/list",
        json={"elementIds": ["property-abc"], "includeMetadata": True},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    result = payload["results"][0]["result"]
    assert result["typeElementId"].startswith("urn:opcua:objecttype:")
    metadata = payload["results"][0]["result"]["metadata"]
    assert metadata["typeNamespaceUri"] == "http://example.com/i3x"
    assert metadata["sourceTypeId"] == "nsu=http://example.com/runtime;s=Temperature"


def test_v1_objects_list_include_metadata_exposes_composition_parent_id(client: TestClient) -> None:
    app = _fastapi_app(client)
    app.state.model_cache.composition_parent_by_id = {
        "sensor-root": "asset-root",
        "property-abc": "asset-root",
    }

    response = client.post(
        "/v1/objects/list",
        json={"elementIds": ["sensor-root"], "includeMetadata": True},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True

    metadata = payload["results"][0]["result"]["metadata"]
    assert metadata["compositionParentId"] == "asset-root"


def test_v1_objects_list_property_null_type_resolves_to_unknown_type(client: TestClient) -> None:
    app = _fastapi_app(client)
    app.state.model_cache.nodes_by_id["property-null-type"] = ModelNode(
        id="property-null-type",
        name="UnknownProperty",
        kind="property",
        type="ns=0;i=0",
        children=[],
        source_node_id="ns=2;s=UnknownProperty",
    )
    app.state.model_cache.children_by_id.setdefault("asset-root", []).append("property-null-type")
    app.state.model_cache.children_by_id["property-null-type"] = []
    app.state.model_cache.property_to_node["property-null-type"] = "ns=2;s=UnknownProperty"

    response = client.post(
        "/v1/objects/list",
        json={"elementIds": ["property-null-type"]},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True

    result = payload["results"][0]["result"]
    assert result["typeElementId"].startswith("urn:opcua:objecttype:")
    assert result["typeElementId"] != "nsu=http://opcfoundation.org/UA/;i=0"

    object_types = client.get("/v1/objecttypes").json()["result"]
    known_type_ids = {item["elementId"] for item in object_types}
    assert result["typeElementId"] in known_type_ids


def test_v1_history_query(client: TestClient) -> None:
    response = client.post(
        "/v1/objects/history",
        json={
            "elementIds": ["property-abc"],
            "startTime": "2026-01-01T00:00:00Z",
            "endTime": "2026-01-02T00:00:00Z",
            "maxDepth": 1,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["results"][0]["success"] is True
    assert payload["results"][0]["result"]["isComposition"] is False
    assert len(payload["results"][0]["result"]["values"]) == 2


def test_v1_history_query_includes_component_histories_when_depth_allows(client: TestClient) -> None:
    app = _fastapi_app(client)
    property_id = "property-abc"
    child_asset_id = "child-asset"
    child_prop_id = "child-prop"

    app.state.model_cache.nodes_by_id[child_asset_id] = ModelNode(
        id=child_asset_id,
        name="ChildAsset",
        kind="asset",
        type="ns=1;i=1001",
        children=[child_prop_id],
        source_node_id="ns=2;s=ChildAsset",
    )
    app.state.model_cache.nodes_by_id[child_prop_id] = ModelNode(
        id=child_prop_id,
        name="ChildProp",
        kind="property",
        type="i=11",
        children=[],
        source_node_id="ns=2;s=ChildProp",
    )
    app.state.model_cache.children_by_id[child_asset_id] = [child_prop_id]
    app.state.model_cache.hierarchy_children_by_id["asset-root"] = [property_id, child_asset_id]
    app.state.model_cache.composition_children_by_id["asset-root"] = [property_id]
    app.state.model_cache.composition_children_by_id[child_asset_id] = [child_prop_id]

    app.state.opcua_client.history_values["ns=2;s=ChildProp"] = [
        SimpleNamespace(
            Value=SimpleNamespace(Value=5.0),
            StatusCode=SimpleNamespace(name="Good"),
            SourceTimestamp=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
            ServerTimestamp=None,
        )
    ]

    response = client.post(
        "/v1/objects/history",
        json={
            "elementIds": ["asset-root"],
            "startTime": "2026-01-01T00:00:00Z",
            "endTime": "2026-01-02T00:00:00Z",
            "maxDepth": 5,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    result = payload["results"][0]["result"]
    component_ids = set((result.get("components") or {}).keys())
    assert property_id in component_ids
    assert child_prop_id not in component_ids
    assert isinstance(result["components"][property_id]["values"], list)


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


def test_v1_value_query_serializes_binary_values(client: TestClient) -> None:
    _fastapi_app(client).state.opcua_client.values["ns=2;s=Temperature"] = b"\xff\x00"

    response = client.post(
        "/v1/objects/value",
        json={
            "elementIds": ["property-abc"],
            "maxDepth": 1,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["results"][0]["success"] is True
    assert payload["results"][0]["result"]["value"] == {
        "encoding": "base64",
        "data": base64.b64encode(b"\xff\x00").decode("ascii"),
    }


def test_v1_value_query_serializes_structured_object_arrays(client: TestClient) -> None:
    async def read_data_values(node_ids: list[str]) -> list[Any]:
        assert node_ids == ["ns=2;s=Temperature"]
        value = [
            FakeExtensionObject(
                "ns=1;i=3001",
                FakeMachineConfig(
                    thresholds=FakeMachineThresholds(min=10.0, max=120.5),
                    mode="auto",
                ),
            ),
            FakeExtensionObject(
                "ns=1;i=3001",
                FakeMachineConfig(
                    thresholds=FakeMachineThresholds(min=12.0, max=130.0),
                    mode="manual",
                ),
            ),
        ]
        return [
            SimpleNamespace(
                Value=SimpleNamespace(Value=value),
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
    assert payload["success"] is True
    assert payload["results"][0]["success"] is True
    assert payload["results"][0]["result"]["value"] == [
        {
            "TypeId": "ns=1;i=3001",
            "Body": {
                "thresholds": {"min": 10.0, "max": 120.5},
                "mode": "auto",
            },
        },
        {
            "TypeId": "ns=1;i=3001",
            "Body": {
                "thresholds": {"min": 12.0, "max": 130.0},
                "mode": "manual",
            },
        },
    ]


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


def test_v1_history_query_serializes_binary_values(client: TestClient) -> None:
    _fastapi_app(client).state.opcua_client.history_values["ns=2;s=Temperature"] = [
        SimpleNamespace(
            Value=SimpleNamespace(Value=b"\xff\x00"),
            StatusCode=SimpleNamespace(name="Good"),
            SourceTimestamp=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
            ServerTimestamp=None,
        )
    ]

    response = client.post(
        "/v1/objects/history",
        json={
            "elementIds": ["property-abc"],
            "startTime": "2026-01-01T00:00:00Z",
            "endTime": "2026-01-02T00:00:00Z",
            "maxDepth": 1,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["results"][0]["success"] is True
    assert payload["results"][0]["result"]["values"][0]["value"] == {
        "encoding": "base64",
        "data": base64.b64encode(b"\xff\x00").decode("ascii"),
    }


def test_expanded_node_id_does_not_rewrite_non_node_ids() -> None:
    namespaces = [OpcUaNamespaceInfo(uri="http://example.com/default", display_name="Default")]
    assert _expanded_node_id("asset-root", namespaces) == "asset-root"


def test_expanded_node_id_rewrites_node_id_strings() -> None:
    namespaces = [
        OpcUaNamespaceInfo(uri="http://example.com/default", display_name="Default"),
        OpcUaNamespaceInfo(uri="http://example.com/custom", display_name="Custom"),
    ]
    assert _expanded_node_id("ns=1;i=1001", namespaces) == "nsu=http://example.com/custom;i=1001"


def test_v1_history_query_missing_object(client: TestClient) -> None:
    response = client.post(
        "/v1/objects/history",
        json={
            "elementIds": ["missing"],
            "startTime": "2026-01-01T00:00:00Z",
            "endTime": "2026-01-02T00:00:00Z",
            "maxDepth": 1,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["results"][0]["success"] is False
    assert payload["results"][0]["error"]["code"] == 404


def test_v1_history_query_invalid_time_range(client: TestClient) -> None:
    response = client.post(
        "/v1/objects/history",
        json={
            "elementIds": ["property-abc"],
            "startTime": "2026-01-02T00:00:00Z",
            "endTime": "2026-01-01T00:00:00Z",
        },
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["code"] == 400


def test_v1_value_query_propagates_source_quality_and_timestamp(client: TestClient) -> None:
    async def read_data_values(node_ids: list[str]) -> list[Any]:
        return [
            SimpleNamespace(
                Value=SimpleNamespace(Value=99.5),
                StatusCode=SimpleNamespace(name="Uncertain", is_good=lambda: False),
                SourceTimestamp=datetime(2026, 3, 15, 8, 30, tzinfo=timezone.utc),
                ServerTimestamp=None,
            )
            for _ in node_ids
        ]

    _fastapi_app(client).state.opcua_client.read_data_values = read_data_values

    response = client.post(
        "/v1/objects/value",
        json={"elementIds": ["property-abc"], "maxDepth": 1},
    )
    assert response.status_code == 200
    payload = response.json()
    result = payload["results"][0]["result"]
    assert result["quality"] == "Uncertain"
    assert result["timestamp"] == "2026-03-15T08:30:00Z"
    assert result["value"] == 99.5


def test_v1_value_query_bad_quality_null_value_is_allowed(client: TestClient) -> None:
    async def read_data_values(node_ids: list[str]) -> list[Any]:
        return [
            SimpleNamespace(
                Value=SimpleNamespace(Value=None),
                StatusCode=SimpleNamespace(name="Bad", is_good=lambda: False),
                SourceTimestamp=datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc),
                ServerTimestamp=None,
            )
            for _ in node_ids
        ]

    _fastapi_app(client).state.opcua_client.read_data_values = read_data_values

    response = client.post(
        "/v1/objects/value",
        json={"elementIds": ["property-abc"], "maxDepth": 1},
    )
    assert response.status_code == 200
    payload = response.json()
    result = payload["results"][0]["result"]
    assert result["quality"] == "Bad"
    assert result["value"] is None


def test_v1_value_query_null_with_good_quality_normalized_to_goodnodata(client: TestClient) -> None:
    async def read_data_values(node_ids: list[str]) -> list[Any]:
        return [
            SimpleNamespace(
                Value=SimpleNamespace(Value=None),
                StatusCode=SimpleNamespace(name="Good", is_good=lambda: True),
                SourceTimestamp=datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc),
                ServerTimestamp=None,
            )
            for _ in node_ids
        ]

    _fastapi_app(client).state.opcua_client.read_data_values = read_data_values

    response = client.post(
        "/v1/objects/value",
        json={"elementIds": ["property-abc"], "maxDepth": 1},
    )
    assert response.status_code == 200
    payload = response.json()
    result = payload["results"][0]["result"]
    assert result["value"] is None
    assert result["quality"] == "GoodNoData"


def test_v1_validation_error_includes_response_detail(client: TestClient) -> None:
    response = client.post(
        "/v1/objects/value",
        json={"maxDepth": 1},
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == 400
    assert payload["responseDetail"]["status"] == 400
    assert payload["responseDetail"]["title"] == "Bad Request"


def test_v1_404_error_includes_response_detail(client: TestClient) -> None:
    response = client.post(
        "/v1/subscriptions/sync",
        json={"clientId": "my-app-instance-001", "subscriptionId": "missing"},
    )
    assert response.status_code == 404
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == 404
    assert payload["responseDetail"]["status"] == 404
    assert payload["responseDetail"]["title"] == "Not Found"


def test_v1_501_error_includes_response_detail(client: TestClient) -> None:
    response = client.put("/v1/objects/some-id/value")
    assert response.status_code == 501
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == 501
    assert payload["responseDetail"]["status"] == 501
    assert payload["responseDetail"]["title"] == "Not Implemented"


def test_v1_update_value_success_when_enabled(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")

    response = client.put("/v1/objects/property-abc/value", json={"value": 55.25})

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["result"] is None
    fake_client = _fastapi_app(client).state.opcua_client
    assert fake_client.values["ns=2;s=Temperature"] == 55.25
    assert fake_client.last_write_variant_type_by_node_id["ns=2;s=Temperature"] == "Double"


def test_v1_update_value_target_not_writable(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")
    _fastapi_app(client).state.opcua_client.writable_by_node_id["ns=2;s=Temperature"] = False

    response = client.put("/v1/objects/property-abc/value", json={"value": 55.25})

    assert response.status_code == 403
    payload = response.json()
    assert payload["error"]["message"] == "target_not_writable"


def test_v1_update_value_rejects_type_mismatch(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")
    _fastapi_app(client).state.opcua_client.variant_type_by_node_id["ns=2;s=Temperature"] = "Double"

    response = client.put("/v1/objects/property-abc/value", json={"value": "bad"})

    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["message"] == "bad_type_or_range"


def test_v1_update_value_maps_opcua_denied_error(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")
    _fastapi_app(client).state.opcua_client.write_failures["ns=2;s=Temperature"] = RuntimeError("BadUserAccessDenied")

    response = client.put("/v1/objects/property-abc/value", json={"value": 55.25})

    assert response.status_code == 403
    payload = response.json()
    assert payload["error"]["message"] == "unauthorized_by_opcua_server"


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


def test_v1_subscription_lifecycle(client: TestClient) -> None:
    client_id = "my-app-instance-001"
    created = client.post(
        "/v1/subscriptions",
        json={"clientId": client_id, "displayName": "Dashboard Monitor"},
    )
    assert created.status_code == 200
    created_payload = created.json()
    subscription_id = created_payload["result"]["subscriptionId"]

    register = client.post(
        "/v1/subscriptions/register",
        json={
            "clientId": client_id,
            "subscriptionId": subscription_id,
            "elementIds": ["property-abc", "ns=2;s=OtherTemp"],
            "maxDepth": 1,
        },
    )
    assert register.status_code == 200
    register_payload = register.json()
    assert register_payload["success"] is False

    listed = client.post(
        "/v1/subscriptions/list",
        json={"clientId": client_id, "subscriptionIds": [subscription_id]},
    )
    assert listed.status_code == 200
    list_payload = listed.json()
    assert list_payload["results"][0]["result"]["subscriptionId"] == subscription_id
    assert list_payload["results"][0]["result"]["mode"] in {"polling", "native"}

    time.sleep(1.2)

    synced = client.post(
        "/v1/subscriptions/sync",
        json={"clientId": client_id, "subscriptionId": subscription_id, "acknowledgeSequence": 0},
    )
    assert synced.status_code == 200
    sync_payload = synced.json()
    assert sync_payload["success"] is True
    assert isinstance(sync_payload["result"], list)
    if sync_payload["result"]:
        assert sync_payload["result"][0]["sequenceNumber"] >= 1
        assert isinstance(sync_payload["result"][0]["updates"], list)
        assert sync_payload["result"][0]["updates"][0]["elementId"]


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


def test_v1_subscription_register_omitted_maxdepth_monitors_descendant_properties(client: TestClient) -> None:
    client_id = "my-app-instance-001"
    app = _fastapi_app(client)
    model = app.state.model_cache
    model.nodes_by_id["asset-child"] = ModelNode(
        id="asset-child",
        name="Nested Asset",
        kind="asset",
        type=None,
        children=["property-def"],
        source_node_id="ns=2;s=NestedAsset",
        source_type_id="ns=1;i=1003",
    )
    model.nodes_by_id["property-def"] = ModelNode(
        id="property-def",
        name="Pressure",
        kind="property",
        type="ns=1;i=11",
        children=[],
        source_node_id="ns=2;s=Pressure",
    )
    model.children_by_id["asset-child"] = ["property-def"]
    model.children_by_id["property-def"] = []
    model.children_by_id["asset-root"] = ["property-abc", "action-def", "asset-child"]
    model.property_to_node["property-def"] = "ns=2;s=Pressure"
    app.state.opcua_client.values["ns=2;s=Pressure"] = 17.5

    created = client.post(
        "/v1/subscriptions",
        json={"clientId": client_id, "displayName": "Deep Monitor"},
    )
    assert created.status_code == 200
    subscription_id = created.json()["result"]["subscriptionId"]

    register = client.post(
        "/v1/subscriptions/register",
        json={
            "clientId": client_id,
            "subscriptionId": subscription_id,
            "elementIds": ["asset-root"],
        },
    )
    assert register.status_code == 200

    synced = client.post(
        "/v1/subscriptions/sync",
        json={"clientId": client_id, "subscriptionId": subscription_id},
    )
    assert synced.status_code == 200
    payload = synced.json()
    assert payload["success"] is True
    assert len(payload["result"]) == 1
    updates = payload["result"][0]["updates"]
    element_ids = {item["elementId"] for item in updates}
    assert {"property-abc", "property-def"}.issubset(element_ids)

    deleted = client.post(
        "/v1/subscriptions/delete",
        json={"clientId": client_id, "subscriptionIds": [subscription_id]},
    )
    assert deleted.status_code == 200
    deleted_payload = deleted.json()
    assert deleted_payload["results"][0]["success"] is True


def test_v1_subscription_sync_serializes_binary_values(client: TestClient) -> None:
    created = client.post(
        "/v1/subscriptions",
        json={"clientId": "my-app-instance-001", "displayName": "Binary Monitor"},
    )
    assert created.status_code == 200
    subscription_id = created.json()["result"]["subscriptionId"]

    service = _fastapi_app(client).state.subscription_service
    state = service._subscriptions[subscription_id]
    service._append_update(state, "ns=2;s=RawBytes", b"\xff\x00")

    synced = client.post(
        "/v1/subscriptions/sync",
        json={
            "clientId": "my-app-instance-001",
            "subscriptionId": subscription_id,
            "acknowledgeSequence": 0,
        },
    )
    assert synced.status_code == 200
    payload = synced.json()
    assert payload["success"] is True
    assert payload["result"][0]["updates"][0]["value"] == {
        "encoding": "base64",
        "data": base64.b64encode(b"\xff\x00").decode("ascii"),
    }


def test_v1_subscription_sync_null_value_uses_goodnodata(client: TestClient) -> None:
    created = client.post(
        "/v1/subscriptions",
        json={"clientId": "my-app-instance-001", "displayName": "Null Monitor"},
    )
    assert created.status_code == 200
    subscription_id = created.json()["result"]["subscriptionId"]

    service = _fastapi_app(client).state.subscription_service
    state = service._subscriptions[subscription_id]
    state.node_to_element_id["ns=2;s=NullValue"] = "ns=2;s=NullValue"

    service._append_update(state, "ns=2;s=NullValue", None)

    synced = client.post(
        "/v1/subscriptions/sync",
        json={
            "clientId": "my-app-instance-001",
            "subscriptionId": subscription_id,
            "acknowledgeSequence": 0,
        },
    )
    assert synced.status_code == 200
    payload = synced.json()
    assert payload["success"] is True
    assert payload["result"][0]["updates"][0]["value"] is None
    assert payload["result"][0]["updates"][0]["quality"] == "GoodNoData"


def test_v1_subscription_sync_rejects_when_stream_active(client: TestClient) -> None:
    created = client.post(
        "/v1/subscriptions",
        json={"clientId": "my-app-instance-001", "displayName": "Stream Lock"},
    )
    assert created.status_code == 200
    subscription_id = created.json()["result"]["subscriptionId"]

    service = _fastapi_app(client).state.subscription_service
    state = service._subscriptions[subscription_id]
    state.stream_connected = True
    state.active_stream_generation = 1

    synced = client.post(
        "/v1/subscriptions/sync",
        json={
            "clientId": "my-app-instance-001",
            "subscriptionId": subscription_id,
            "acknowledgeSequence": 0,
        },
    )
    assert synced.status_code == 400
    payload = synced.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == 400

    state.stream_connected = False


def test_v1_subscription_sync_returns_206_on_overflow(client: TestClient) -> None:
    created = client.post(
        "/v1/subscriptions",
        json={"clientId": "my-app-instance-001", "displayName": "Overflow Monitor"},
    )
    assert created.status_code == 200
    subscription_id = created.json()["result"]["subscriptionId"]

    service = _fastapi_app(client).state.subscription_service
    service._max_updates_per_subscription = 2
    state = service._subscriptions[subscription_id]
    state.node_to_element_id["ns=2;s=Overflow1"] = "ns=2;s=Overflow1"
    state.node_to_element_id["ns=2;s=Overflow2"] = "ns=2;s=Overflow2"
    state.node_to_element_id["ns=2;s=Overflow3"] = "ns=2;s=Overflow3"

    service._append_update(state, "ns=2;s=Overflow1", 1)
    service._append_update(state, "ns=2;s=Overflow2", 2)
    service._append_update(state, "ns=2;s=Overflow3", 3)

    synced = client.post(
        "/v1/subscriptions/sync",
        json={
            "clientId": "my-app-instance-001",
            "subscriptionId": subscription_id,
            "acknowledgeSequence": 0,
        },
    )
    assert synced.status_code == 206
    payload = synced.json()
    assert payload["success"] is True
    assert isinstance(payload["result"], list)
    assert payload["responseDetail"]["status"] == 206
    assert "Dropped sequence numbers" in payload["responseDetail"]["detail"]


def test_v1_subscription_stream_not_found(client: TestClient) -> None:
    response = client.post(
        "/v1/subscriptions/stream",
        json={"clientId": "my-app-instance-001", "subscriptionId": "missing"},
    )
    assert response.status_code == 404


def test_v1_subscription_stream_not_found_with_ack_fields(client: TestClient) -> None:
    response_ack = client.post(
        "/v1/subscriptions/stream",
        json={"clientId": "my-app-instance-001", "subscriptionId": "missing", "acknowledgeSequence": 4},
    )
    assert response_ack.status_code == 404

    response_legacy = client.post(
        "/v1/subscriptions/stream",
        json={"clientId": "my-app-instance-001", "subscriptionId": "missing", "lastSequenceNumber": 4},
    )
    assert response_legacy.status_code == 404


def test_openapi_json_is_source_of_truth(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200

    generated = response.json()
    assert isinstance(generated, dict)
    assert isinstance(generated.get("paths"), dict)


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
        if tool_name == "updateObjectValue":
            body_dict = {"value": 123}
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


def test_mcp_tool_input_schemas_match_openapi_contract(client: TestClient) -> None:
    openapi = client.get("/openapi.json").json()
    tools_payload = client.get("/mcp/tools").json()
    tools = tools_payload["tools"]

    components = openapi.get("components", {})
    assert isinstance(components, Mapping)

    paths = openapi.get("paths", {})
    assert isinstance(paths, Mapping)

    for path, methods in paths.items():
        if not isinstance(path, str) or not isinstance(methods, Mapping):
            continue
        if path.startswith("/mcp"):
            continue

        for method, details in methods.items():
            if not isinstance(method, str) or not isinstance(details, Mapping):
                continue
            operation_id = details.get("operationId")
            if not isinstance(operation_id, str) or path.endswith("/subscriptions/stream"):
                continue
            if method.upper() == "PUT":
                continue

            assert operation_id in tools, f"Missing MCP tool for operationId={operation_id}"
            tool = tools[operation_id]

            assert tool["method"] == method.upper()
            assert tool["path"] == path

            input_schema = tool.get("inputSchema", {})
            assert input_schema.get("type") == "object"
            assert input_schema.get("additionalProperties") is False

            properties = input_schema.get("properties", {})
            required = set(input_schema.get("required", []))
            assert isinstance(properties, Mapping)

            expected_required: set[str] = set()
            expected_property_names: set[str] = set()

            for parameter in details.get("parameters", []):
                if not isinstance(parameter, Mapping):
                    continue
                parameter_name = parameter.get("name")
                if not isinstance(parameter_name, str):
                    continue
                expected_property_names.add(parameter_name)
                expected_schema = _resolve_openapi_schema(parameter.get("schema", {"type": "string"}), components)
                assert properties.get(parameter_name) == expected_schema
                if parameter.get("required"):
                    expected_required.add(parameter_name)

            request_body = details.get("requestBody")
            if isinstance(request_body, Mapping):
                content = request_body.get("content", {})
                if isinstance(content, Mapping):
                    app_json = content.get("application/json")
                    if isinstance(app_json, Mapping) and "schema" in app_json:
                        expected_property_names.add("body")
                        expected_body_schema = _resolve_openapi_schema(app_json["schema"], components)
                        assert properties.get("body") == expected_body_schema
                        if request_body.get("required"):
                            expected_required.add("body")

            assert set(properties.keys()) == expected_property_names
            assert required == expected_required


def test_mcp_non_subscription_tools_runtime_smoke(client: TestClient) -> None:
    tools_response = client.get("/mcp/tools")
    assert tools_response.status_code == 200
    tools = tools_response.json()["tools"]

    skipped_tools = {
        _operation_id_for(client, "POST", "/v1/subscriptions"),
        _operation_id_for(client, "POST", "/v1/subscriptions/register"),
        _operation_id_for(client, "POST", "/v1/subscriptions/unregister"),
        _operation_id_for(client, "POST", "/v1/subscriptions/sync"),
        _operation_id_for(client, "POST", "/v1/subscriptions/delete"),
        _operation_id_for(client, "POST", "/v1/subscriptions/list"),
    }

    for tool_name, tool in tools.items():
        if tool_name in skipped_tools:
            continue

        input_schema = tool.get("inputSchema", {})
        assert isinstance(input_schema, Mapping)
        arguments = _build_required_mcp_arguments(input_schema)
        arguments = _with_runtime_argument_overrides(tool_name, arguments, subscription_id=None)

        response = client.post("/mcp/call", json={"tool": tool_name, "arguments": arguments})
        assert response.status_code in {200, 206, 404, 501}, (
            f"Unexpected status for {tool_name} with args {arguments}: {response.status_code} {response.text}"
        )

        if response.status_code == 400:
            payload = response.json()
            message = payload.get("error", {}).get("message", "")
            assert "Missing required arguments" not in message
            assert "Unexpected arguments" not in message
            assert "Missing required body" not in message


def test_mcp_subscription_tools_runtime_lifecycle(client: TestClient) -> None:
    create_subscription_tool = _operation_id_for(client, "POST", "/v1/subscriptions")
    register_tool = _operation_id_for(client, "POST", "/v1/subscriptions/register")
    list_tool = _operation_id_for(client, "POST", "/v1/subscriptions/list")
    sync_tool = _operation_id_for(client, "POST", "/v1/subscriptions/sync")
    unregister_tool = _operation_id_for(client, "POST", "/v1/subscriptions/unregister")
    delete_tool = _operation_id_for(client, "POST", "/v1/subscriptions/delete")

    create_response = client.post(
        "/mcp/call",
        json={
            "tool": create_subscription_tool,
            "arguments": {"body": {"clientId": "mcp-runtime-smoke", "displayName": "MCP Runtime Smoke"}},
        },
    )
    assert create_response.status_code == 200
    create_payload = create_response.json()
    subscription_id = create_payload["result"]["subscriptionId"]

    register_response = client.post(
        "/mcp/call",
        json={
            "tool": register_tool,
            "arguments": {
                "body": {
                    "clientId": "mcp-runtime-smoke",
                    "subscriptionId": subscription_id,
                    "elementIds": ["property-abc"],
                    "maxDepth": 1,
                }
            },
        },
    )
    assert register_response.status_code == 200

    list_response = client.post(
        "/mcp/call",
        json={
            "tool": list_tool,
            "arguments": {"body": {"clientId": "mcp-runtime-smoke", "subscriptionIds": [subscription_id]}},
        },
    )
    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert list_payload["results"][0]["result"]["subscriptionId"] == subscription_id

    sync_response = client.post(
        "/mcp/call",
        json={
            "tool": sync_tool,
            "arguments": {
                "body": {
                    "clientId": "mcp-runtime-smoke",
                    "subscriptionId": subscription_id,
                    "lastSequenceNumber": 0,
                }
            },
        },
    )
    assert sync_response.status_code == 200

    remove_response = client.post(
        "/mcp/call",
        json={
            "tool": unregister_tool,
            "arguments": {
                "body": {
                    "clientId": "mcp-runtime-smoke",
                    "subscriptionId": subscription_id,
                    "elementIds": ["property-abc"],
                    "maxDepth": 1,
                }
            },
        },
    )
    assert remove_response.status_code == 200

    delete_response = client.post(
        "/mcp/call",
        json={
            "tool": delete_tool,
            "arguments": {"body": {"clientId": "mcp-runtime-smoke", "subscriptionIds": [subscription_id]}},
        },
    )
    assert delete_response.status_code == 200
    delete_payload = delete_response.json()
    assert delete_payload["results"][0]["success"] is True


def test_mcp_tools_are_generated_from_openapi(client_without_tool_overrides: TestClient) -> None:
    response = client_without_tool_overrides.get("/mcp/tools")
    assert response.status_code == 200

    payload = response.json()
    tools = payload["tools"]
    namespaces_id = _operation_id_for(client_without_tool_overrides, "GET", "/v1/namespaces")
    query_values_id = _operation_id_for(client_without_tool_overrides, "POST", "/v1/objects/value")
    stream_id = _operation_id_for(client_without_tool_overrides, "POST", "/v1/subscriptions/stream")
    assert namespaces_id in tools
    assert query_values_id in tools
    assert stream_id not in tools

    namespaces_tool = tools[namespaces_id]
    assert isinstance(namespaces_tool.get("description"), str)
    assert namespaces_tool["description"]
    assert namespaces_tool.get("priority") == "normal"
    assert namespaces_tool.get("keywords") == []

    value_tool = tools[query_values_id]
    assert value_tool["method"] == "POST"
    assert value_tool["path"] == "/v1/objects/value"
    assert value_tool["input_schema"]["properties"]["body"]["properties"]["elementIds"]["type"] == "array"
    assert value_tool["inputSchema"]["properties"]["body"]["properties"]["elementIds"]["type"] == "array"


def test_mcp_write_tools_hidden(client: TestClient) -> None:
    tools = client.get("/mcp/tools").json()["tools"]
    update_value_id = _operation_id_for(client, "PUT", "/v1/objects/{element_id}/value")
    assert update_value_id not in tools


def test_mcp_update_history_tool_hidden(client: TestClient) -> None:
    tools = client.get("/mcp/tools").json()["tools"]
    update_history_id = _operation_id_for(client, "PUT", "/v1/objects/{element_id}/history")
    assert update_history_id not in tools


def test_mcp_tool_overrides_match_live_tools(client: TestClient) -> None:
    overrides = load_tool_overrides()
    tools = client.get("/mcp/tools").json()["tools"]

    unknown_overrides = sorted(set(overrides) - set(tools))
    assert unknown_overrides == []

    for tool_name, override in overrides.items():
        tool = tools[tool_name]
        assert tool["description"] == override["description"]
        assert tool["priority"] == override.get("priority", "normal")
        assert tool["keywords"] == override.get("keywords", [])


def test_mcp_support_is_disabled_by_default(client_without_mcp: TestClient) -> None:
    response = client_without_mcp.get("/mcp")
    assert response.status_code == 404

    response = client_without_mcp.get("/mcp/tools")
    assert response.status_code == 404

    openapi = client_without_mcp.get("/openapi.json").json()
    assert not any(path.startswith("/mcp") for path in openapi["paths"])


def test_mcp_endpoint_exposes_sse_discovery(client: TestClient) -> None:
    response = client.get("/mcp")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: endpoint" in response.text
    assert "/mcp" in response.text
    assert '"method": "notifications/prompts/list_changed"' in response.text
    assert '"method": "notifications/resources/list_changed"' in response.text
    assert '"method": "notifications/roots/list_changed"' in response.text


def test_mcp_initialize_request(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 1
    assert payload["result"]["protocolVersion"] == "2025-06-18"
    assert payload["result"]["capabilities"]["tools"]["listChanged"] is False
    assert payload["result"]["capabilities"]["prompts"]["listChanged"] is True
    assert payload["result"]["capabilities"]["resources"]["listChanged"] is True
    assert payload["result"]["capabilities"]["roots"]["listChanged"] is True


def test_mcp_tools_list_request(client: TestClient) -> None:
    namespaces_id = _operation_id_for(client, "GET", "/v1/namespaces")
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert response.status_code == 200
    payload = response.json()
    tools = payload["result"]["tools"]
    assert any(tool["name"] == namespaces_id for tool in tools)


def test_mcp_prompts_list_rest(client: TestClient) -> None:
    response = client.get("/mcp/prompts")
    assert response.status_code == 200
    payload = response.json()
    prompts = payload["prompts"]
    assert any(item["name"] == "machine_health_snapshot" for item in prompts)
    assert any(item["name"] == "alarm_triage" for item in prompts)


def test_mcp_prompts_get_rest(client: TestClient) -> None:
    response = client.get("/mcp/prompts/machine_health_snapshot")
    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "machine_health_snapshot"
    assert payload["inputs"] == ["asset_id", "lookback_minutes"]
    assert "{{asset_id}}" in payload["template"]


def test_mcp_prompts_execute_rest(client: TestClient) -> None:
    response = client.post(
        "/mcp/prompts/execute",
        json={
            "name": "machine_health_snapshot",
            "parameters": {"asset_id": "Press-01", "lookback_minutes": "60"},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "machine_health_snapshot"
    assert "Press-01" in payload["rendered"]


def test_mcp_prompts_list_request(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 21, "method": "prompts/list"},
    )
    assert response.status_code == 200
    payload = response.json()
    prompts = payload["result"]["prompts"]
    assert any(prompt["name"] == "machine_health_snapshot" for prompt in prompts)


def test_mcp_resources_list_rest(client: TestClient) -> None:
    response = client.get("/mcp/resources")
    assert response.status_code == 200
    payload = response.json()
    resources = payload["resources"]
    assert any(item["uri"] == "i3x://openapi" for item in resources)
    assert any(item["uri"] == "i3x://mcp-overrides" for item in resources)


def test_mcp_resource_read_rest(client: TestClient) -> None:
    response = client.post("/mcp/resources/read", json={"uri": "i3x://openapi"})
    assert response.status_code == 200
    payload = response.json()
    contents = payload["contents"]
    assert len(contents) == 1
    assert contents[0]["uri"] == "i3x://openapi"
    assert contents[0]["mimeType"] == "application/json"


def test_mcp_roots_list_rest(client: TestClient) -> None:
    response = client.get("/mcp/roots")
    assert response.status_code == 200
    payload = response.json()
    roots = payload["roots"]
    assert any(item["uri"] == "i3x://roots/asset-root" for item in roots)


def test_mcp_resources_list_request(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 24, "method": "resources/list"},
    )
    assert response.status_code == 200
    payload = response.json()
    resources = payload["result"]["resources"]
    assert any(item["uri"] == "i3x://docs/quick-reference" for item in resources)


def test_mcp_resources_read_request(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 25,
            "method": "resources/read",
            "params": {"uri": "i3x://mcp-overrides"},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    contents = payload["result"]["contents"]
    assert len(contents) == 1
    assert contents[0]["uri"] == "i3x://mcp-overrides"


def test_mcp_roots_list_request(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 26, "method": "roots/list"},
    )
    assert response.status_code == 200
    payload = response.json()
    roots = payload["result"]["roots"]
    assert any(item["uri"] == "i3x://roots/asset-root" for item in roots)


def test_mcp_batch_request_returns_multiple_results(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json=[
            {"jsonrpc": "2.0", "id": 27, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 28, "method": "prompts/list"},
        ],
    )
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    assert len(payload) == 2
    ids = {item["id"] for item in payload}
    assert ids == {27, 28}


def test_mcp_empty_batch_returns_invalid_request(client: TestClient) -> None:
    response = client.post("/mcp", json=[])
    assert response.status_code == 200
    payload = response.json()
    assert payload["error"]["code"] == -32600


def test_mcp_invalid_jsonrpc_version_returns_invalid_request(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "1.0", "id": 29, "method": "tools/list"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == 29
    assert payload["error"]["code"] == -32600


def test_mcp_prompts_get_request(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 22,
            "method": "prompts/get",
            "params": {"name": "machine_health_snapshot"},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    prompt = payload["result"]
    assert prompt["name"] == "machine_health_snapshot"
    assert prompt["inputs"] == ["asset_id", "lookback_minutes"]


def test_mcp_prompts_execute_request(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 23,
            "method": "prompts/execute",
            "params": {
                "name": "machine_health_snapshot",
                "parameters": {"asset_id": "Press-01", "lookback_minutes": "60"},
            },
        },
    )
    assert response.status_code == 200
    payload = response.json()
    result = payload["result"]
    assert result["name"] == "machine_health_snapshot"
    assert "Press-01" in result["rendered"]


def test_mcp_tools_call_request(client: TestClient) -> None:
    namespaces_id = _operation_id_for(client, "GET", "/v1/namespaces")
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": namespaces_id, "arguments": {}},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    content = payload["result"]["content"]
    assert content[0]["type"] == "text"
    assert "success" in content[0]["text"]


def test_mcp_initialize_notification_returns_no_response(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
    )

    assert response.status_code == 202
    assert response.text == "null"


def test_mcp_tools_list_notification_returns_no_response(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "method": "tools/list",
        },
    )

    assert response.status_code == 202
    assert response.text == "null"


def test_mcp_tools_call_notification_returns_no_response(client: TestClient) -> None:
    namespaces_id = _operation_id_for(client, "GET", "/v1/namespaces")
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": namespaces_id, "arguments": {}},
        },
    )

    assert response.status_code == 202
    assert response.text == "null"


def test_mcp_call_allows_omitting_optional_query_parameters(client: TestClient) -> None:
    tools_response = client.get("/mcp/tools")
    assert tools_response.status_code == 200
    tools = tools_response.json()["tools"]

    candidate_name: str | None = None
    for name, tool in tools.items():
        query_parameters = set(tool.get("query_parameters", []))
        required_fields = set(tool.get("input_schema", {}).get("required", []))
        required_query_parameters = query_parameters & required_fields
        if (
            query_parameters
            and not required_query_parameters
            and not tool.get("path_parameters")
            and not tool.get("body_required", False)
            and tool.get("method") == "GET"
        ):
            candidate_name = name
            break

    if candidate_name is None:
        pytest.skip("No MCP tool with fully optional query parameters is available")

    response = client.post("/mcp/call", json={"tool": candidate_name, "arguments": {}})
    assert response.status_code == 200


def test_mcp_jsonrpc_tools_call_returns_jsonrpc_error_for_http_exception(client: TestClient) -> None:
    list_by_id = _operation_id_for(client, "POST", "/v1/objects/list")
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 301,
            "method": "tools/call",
            "params": {"name": list_by_id, "arguments": {}},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 301
    assert payload["error"]["code"] == 400
    assert payload["error"]["code"] in {400, -32602}
    assert "Missing required" in payload["error"]["message"]


def test_mcp_call_dispatches_to_existing_api(client: TestClient) -> None:
    namespaces_id = _operation_id_for(client, "GET", "/v1/namespaces")
    response = client.post("/mcp/call", json={"tool": namespaces_id, "arguments": {}})
    assert response.status_code == 200

    expected = client.get("/v1/namespaces")
    assert response.json() == expected.json()


def test_mcp_call_supports_body_arguments(client: TestClient) -> None:
    query_values_id = _operation_id_for(client, "POST", "/v1/objects/value")
    response = client.post(
        "/mcp/call",
        json={
            "tool": query_values_id,
            "arguments": {
                "body": {
                    "elementIds": ["property-abc"],
                    "maxDepth": 1,
                },
            },
        },
    )
    assert response.status_code == 200

    payload = response.json()
    assert payload["success"] is True
    assert payload["results"][0]["success"] is True
    assert payload["results"][0]["result"]["isComposition"] is False


@pytest.mark.parametrize("element_id", ["http://evil.example", "../evil"])
def test_mcp_call_rejects_malicious_path_parameters(client: TestClient, element_id: str) -> None:
    history_tool = _operation_id_for(client, "GET", "/v1/objects/{element_id}/history")
    response = client.post(
        "/mcp/call",
        json={"tool": history_tool, "arguments": {"element_id": element_id}},
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["message"] == "Invalid path parameter: element_id"


def test_mcp_call_rejects_unknown_tool(client: TestClient) -> None:
    response = client.post("/mcp/call", json={"tool": "unknownTool", "arguments": {}})
    assert response.status_code == 400


def test_mcp_get_api_prefix_strips_host_parts() -> None:
    openapi_spec = {"servers": [{"url": "https://example.test/v1"}]}
    assert get_api_prefix(openapi_spec) == "/v1"


@pytest.mark.parametrize("path", ["http://evil.example/pwn", "//evil.example/pwn"])
def test_mcp_internal_request_url_rejects_external_hosts(path: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        _safe_internal_request_url(path)
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["error"]["message"] in {"Invalid MCP request path", "Invalid MCP tool path"}


def test_mcp_internal_request_url_keeps_fixed_internal_host() -> None:
    url = _safe_internal_request_url("/v1/namespaces")
    assert url.host == "mcp.local"
    assert str(url) == "http://mcp.local/v1/namespaces"


def test_mcp_call_strips_host_from_runtime_api_prefix(client: TestClient) -> None:
    app = _fastapi_app(client)
    app.state.mcp_api_prefix = "https://evil.example"

    namespaces_id = _operation_id_for(client, "GET", "/v1/namespaces")
    response = client.post("/mcp/call", json={"tool": namespaces_id, "arguments": {}})
    assert response.status_code == 200
