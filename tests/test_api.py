from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Generator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from asyncua import ua
from fastapi import FastAPI
from fastapi.testclient import TestClient

from i3x_server.api.beta import _expanded_node_id
from i3x_server.main import create_app
from i3x_server.opcua.client import OpcUaNamespaceInfo, OpcUaSubscriptionCapabilities
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult
from i3x_server.subscriptions.service import SubscriptionService


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


def _fastapi_app(client: TestClient) -> FastAPI:
    return cast(FastAPI, client.app)


class FakeOpcUaClient:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {"ns=2;s=Temperature": 42.5}
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

    async def read_history_values(
        self,
        node_ids: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> dict[str, list[Any]]:
        del start_time, end_time
        return {node_id: self.history_values.get(node_id, []) for node_id in node_ids}

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
    os.environ["I3X_SKIP_OPCUA_CONNECT"] = "1"
    app = create_app()

    property_id = "property-abc"
    action_id = "action-def"
    root_id = "asset-root"

    with TestClient(app) as test_client:
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
        yield test_client


def test_get_model(client: TestClient) -> None:
    response = client.get("/model")
    assert response.status_code == 404


def test_get_data_value(client: TestClient) -> None:
    response = client.get("/data/property-abc")
    assert response.status_code == 404


def test_invoke_action(client: TestClient) -> None:
    response = client.post("/action/action-def/invoke", json={"args": [1, "x"]})
    assert response.status_code == 404


def test_beta_info(client: TestClient) -> None:
    response = client.get("/v1/info")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["result"]["specVersion"] == "1.0"
    assert payload["result"]["capabilities"]["query"]["history"] is True
    assert payload["result"]["capabilities"]["subscribe"]["stream"] is True


def test_beta_namespaces(client: TestClient) -> None:
    response = client.get("/v1/namespaces")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert len(payload["result"]) == 3


def test_beta_objecttypes(client: TestClient) -> None:
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
    assert first["schema"]["x-opcua-description"] == "Machine object type"
    assert first["schema"]["x-opcua-isAbstract"] is False
    assert isinstance(first["schema"]["properties"], dict)
    assert first["schema"]["properties"]["temperature"]["type"] == "number"
    assert first["schema"]["properties"]["running"]["type"] == "boolean"
    assert first["schema"]["properties"]["temperature"]["x-opcua-nodeId"].startswith("nsu=http://example.com/custom;")
    assert first["schema"]["properties"]["temperature"]["x-opcua-displayName"] == "Temperature"
    assert first["schema"]["properties"]["temperature"]["x-opcua-description"] == "Current measured temperature"
    assert first["schema"]["properties"]["temperature"]["x-opcua-modellingRule"] == "Mandatory"
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
    assert first["related"]["instances"][0]["metadata"]["relationships"]["HasChildren"] == [
        "property-abc",
        "action-def",
    ]

    synthetic = next(
        item for item in payload["result"] if item["sourceTypeId"] == "nsu=http://example.com/custom;i=3001"
    )
    assert synthetic["elementId"].startswith("urn:opcua:objecttype:")
    assert synthetic["namespaceUri"] == "http://example.com/custom"
    assert synthetic["displayName"] == "FakeMachineConfig"
    assert synthetic["schema"]["type"] == "object"
    assert synthetic["schema"]["x-opcua-structureTypeId"] == "nsu=http://example.com/custom;i=3001"
    assert synthetic["schema"]["x-opcua-nodeId"] == "nsu=http://example.com/custom;i=3001"
    synthetic_thresholds_schema = synthetic["schema"]["properties"]["thresholds"]
    assert isinstance(synthetic_thresholds_schema.get("$ref"), str)
    synthetic_thresholds_ref = synthetic_thresholds_schema["$ref"]
    assert synthetic_thresholds_ref.startswith("#/$defs/")
    synthetic_thresholds_def_key = synthetic_thresholds_ref.split("#/$defs/", 1)[1]
    assert synthetic["schema"]["$defs"][synthetic_thresholds_def_key]["properties"]["min"]["type"] == "number"
    assert synthetic["schema"]["$defs"][synthetic_thresholds_def_key]["properties"]["max"]["type"] == "number"

    second = payload["result"][1]
    assert [item["elementId"] for item in second["related"]["instances"]] == ["sensor-root"]


def test_beta_objecttypes_namespace_uri_is_declared(client: TestClient) -> None:
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


def test_beta_objecttypes_namespace_filter_is_canonicalized(client: TestClient) -> None:
    response = client.get("/v1/objecttypes", params={"namespaceUri": "HTTP://EXAMPLE.COM/CUSTOM/"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert len(payload["result"]) > 0
    assert all(item["namespaceUri"] == "http://example.com/custom" for item in payload["result"])


def test_beta_objecttypes_query(client: TestClient) -> None:
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


def test_beta_objecttypes_includes_builtin_scalar_datatype_reference(client: TestClient) -> None:
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


def test_beta_objecttypes_includes_builtin_localizedtext_structured_schema(client: TestClient) -> None:
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
    assert localized_text["schema"]["type"] == "object"
    assert set(localized_text["schema"]["properties"]["Locale"]["type"]) == {"null", "string"}
    assert set(localized_text["schema"]["properties"]["Text"]["type"]) == {"null", "string"}


def test_beta_objecttypes_resolves_standard_structured_datatype(client: TestClient) -> None:
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
        assert resolved["schema"]["type"] == "object"
        assert resolved["schema"]["x-opcua-structureDataType"] == "nsu=http://opcfoundation.org/UA/;i=96"
    finally:
        ua_any.extension_objects_by_datatype = previous_registry


def test_beta_objecttypes_registers_source_type_alias_element_id(client: TestClient) -> None:
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


def test_beta_objecttypes_does_not_register_action_source_node_id_as_object_type(client: TestClient) -> None:
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


def test_beta_objecttypes_registers_standard_ua_optionset_datatype_as_known(client: TestClient) -> None:
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
    assert resolved["schema"]["oneOf"][0]["type"] == "integer"


def test_beta_objecttypes_registers_standard_ua_structured_datatype_as_known(client: TestClient) -> None:
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
    assert resolved["schema"]["type"] == "object"


def test_beta_objecttypes_registers_standard_ua_role_permission_as_known(client: TestClient) -> None:
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
    assert resolved["schema"]["type"] == "object"


def test_beta_objecttypes_registers_generic_custom_nodeid_type_as_known(client: TestClient) -> None:
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


def test_beta_objecttypes_unresolved_standard_property_datatype_gets_fallback_schema(client: TestClient) -> None:
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


def test_beta_objecttypes_generic_standard_id_uses_browse_name_lookup(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("i3x_server.api.beta._ENABLE_LIVE_TYPE_NAME_LOOKUP", True)
    monkeypatch.setattr("i3x_server.api.beta._LIVE_TYPE_NAME_LOOKUP_MAX_PER_REQUEST", 100)

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


def test_beta_objecttypes_does_not_publish_null_opcua_type_id(client: TestClient) -> None:
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


def test_beta_relationshiptypes(client: TestClient) -> None:
    response = client.get("/v1/relationshiptypes")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert len(payload["result"]) >= 4
    ids = {item["elementId"] for item in payload["result"]}
    assert "HasComponent" in ids
    assert "HasParent" in ids


def test_beta_relationshiptypes_query(client: TestClient) -> None:
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


def test_beta_objects_list(client: TestClient) -> None:
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


def test_beta_objects_list_include_metadata_uses_expanded_source_type_id(client: TestClient) -> None:
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


def test_beta_objects_list_property_null_type_resolves_to_unknown_type(client: TestClient) -> None:
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


def test_beta_history_query(client: TestClient) -> None:
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


def test_beta_value_query_serializes_binary_values(client: TestClient) -> None:
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


def test_beta_value_query_serializes_structured_object_arrays(client: TestClient) -> None:
    async def read_values(node_ids: list[str]) -> list[Any]:
        assert node_ids == ["ns=2;s=Temperature"]
        return [
            [
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
        ]

    _fastapi_app(client).state.opcua_client.read_values = read_values

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


def test_beta_history_query_serializes_binary_values(client: TestClient) -> None:
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


def test_beta_history_query_missing_object(client: TestClient) -> None:
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


def test_beta_history_query_invalid_time_range(client: TestClient) -> None:
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


def test_beta_subscription_lifecycle(client: TestClient) -> None:
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
        assert sync_payload["result"][0]["elementId"]

    deleted = client.post(
        "/v1/subscriptions/delete",
        json={"clientId": client_id, "subscriptionIds": [subscription_id]},
    )
    assert deleted.status_code == 200
    deleted_payload = deleted.json()
    assert deleted_payload["results"][0]["success"] is True


def test_beta_subscription_sync_serializes_binary_values(client: TestClient) -> None:
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
    assert payload["result"][0]["value"] == {
        "encoding": "base64",
        "data": base64.b64encode(b"\xff\x00").decode("ascii"),
    }


def test_beta_subscription_stream_not_found(client: TestClient) -> None:
    response = client.post(
        "/v1/subscriptions/stream",
        json={"clientId": "my-app-instance-001", "subscriptionId": "missing"},
    )
    assert response.status_code == 404


def test_beta_subscription_stream_not_found_with_ack_fields(client: TestClient) -> None:
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

    expected_path = Path(__file__).resolve().parents[1] / "openapi.json"
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    assert response.json() == expected


def test_mcp_tools_are_generated_from_openapi(client: TestClient) -> None:
    response = client.get("/mcp/tools")
    assert response.status_code == 200

    payload = response.json()
    tools = payload["tools"]
    assert "getNamespaces" in tools
    assert "queryLastKnownValues" in tools
    assert "streamSubscription" not in tools

    value_tool = tools["queryLastKnownValues"]
    assert value_tool["method"] == "POST"
    assert value_tool["path"] == "/objects/value"
    assert value_tool["input_schema"]["properties"]["body"]["properties"]["elementIds"]["type"] == "array"


def test_mcp_endpoint_exposes_sse_discovery(client: TestClient) -> None:
    response = client.get("/mcp")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: endpoint" in response.text
    assert "/mcp" in response.text


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


def test_mcp_tools_list_request(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert response.status_code == 200
    payload = response.json()
    tools = payload["result"]["tools"]
    assert any(tool["name"] == "getNamespaces" for tool in tools)


def test_mcp_tools_call_request(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "getNamespaces", "arguments": {}},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    content = payload["result"]["content"]
    assert content[0]["type"] == "text"
    assert "success" in content[0]["text"]


def test_mcp_call_dispatches_to_existing_api(client: TestClient) -> None:
    response = client.post("/mcp/call", json={"tool": "getNamespaces", "arguments": {}})
    assert response.status_code == 200

    expected = client.get("/v1/namespaces")
    assert response.json() == expected.json()


def test_mcp_call_supports_body_arguments(client: TestClient) -> None:
    response = client.post(
        "/mcp/call",
        json={
            "tool": "queryLastKnownValues",
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


def test_mcp_call_rejects_unknown_tool(client: TestClient) -> None:
    response = client.post("/mcp/call", json={"tool": "unknownTool", "arguments": {}})
    assert response.status_code == 400
