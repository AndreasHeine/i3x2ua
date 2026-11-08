"""ObjectTypes endpoints tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, cast

import pytest
from asyncua import ua
from fastapi import FastAPI
from fastapi.testclient import TestClient

from i3x_server.schemas.i3x import ModelNode
from tests.conftest import fastapi_app


def test_v1_objecttypes(client: TestClient) -> None:
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
    assert first["schema"]["x-opcua-isAbstract"] is False
    assert isinstance(first["schema"]["properties"], dict)
    assert first["schema"]["properties"]["temperature"]["type"] == "number"
    assert first["schema"]["properties"]["running"]["type"] == "boolean"
    assert first["schema"]["properties"]["temperature"]["x-opcua-nodeId"].startswith("nsu=http://example.com/custom;")
    assert first["schema"]["properties"]["temperature"]["x-opcua-displayName"] == "Temperature"
    assert first["schema"]["properties"]["temperature"]["description"] == "Current measured temperature"
    assert "x-opcua-description" not in first["schema"]["properties"]["temperature"]
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
    app = fastapi_app(client)
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
    assert builtin["elementId"].startswith("urn:") and ":objecttype:" in builtin["elementId"]
    assert builtin["displayName"] != "UnknownType"
    assert builtin["schema"]["oneOf"][0]["type"] == "null"
    assert builtin["schema"]["oneOf"][1]["type"] == "string"
    assert builtin["schema"]["oneOf"][2] == {"type": "array", "items": {"type": ["string", "null"]}}


def test_v1_objecttypes_includes_builtin_localizedtext_structured_schema(client: TestClient) -> None:
    app = fastapi_app(client)
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

    app = fastapi_app(client)
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
    app = fastapi_app(client)
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
        (item for item in payload["result"] if item["sourceTypeId"] == "nsu=http://example.com/custom;i=3001"),
        None,
    )
    assert alias is not None
    assert alias["displayName"] == "FakeMachineConfig"
    assert alias["sourceTypeId"] == "nsu=http://example.com/custom;i=3001"
    assert alias["elementId"].startswith("urn:test-server:objecttype:")
    assert alias["elementId"] != alias["sourceTypeId"]
    assert alias["schema"]["x-opcua-nodeId"] == "nsu=http://example.com/custom;i=3001"


def test_v1_objecttypes_does_not_register_action_source_node_id_as_object_type(client: TestClient) -> None:
    app = fastapi_app(client)
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
    app = fastapi_app(client)
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
    assert resolved["elementId"].startswith("urn:") and ":objecttype:" in resolved["elementId"]
    assert resolved["displayName"] == "AccessRestrictionType"
    assert resolved["schema"]["title"] == "AccessRestrictionType"
    assert resolved["schema"]["oneOf"][0]["type"] == "null"
    assert resolved["schema"]["oneOf"][1]["type"] == "integer"


def test_v1_objecttypes_registers_standard_ua_structured_datatype_as_known(client: TestClient) -> None:
    app = fastapi_app(client)
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
    assert resolved["elementId"].startswith("urn:") and ":objecttype:" in resolved["elementId"]
    assert resolved["displayName"] == "SessionDiagnosticsDataType"
    assert resolved["schema"]["title"] == "SessionDiagnosticsDataType"
    session_ref = resolved["schema"]["oneOf"][1]["$ref"]
    assert session_ref.startswith("#/$defs/")
    assert resolved["schema"]["$defs"][session_ref.split("#/$defs/", 1)[1]]["type"] == "object"


def test_v1_objecttypes_registers_standard_ua_role_permission_as_known(client: TestClient) -> None:
    app = fastapi_app(client)
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
    assert resolved["elementId"].startswith("urn:") and ":objecttype:" in resolved["elementId"]
    assert resolved["displayName"] == "RolePermissionType"
    assert resolved["schema"]["title"] == "RolePermissionType"
    role_ref = resolved["schema"]["oneOf"][1]["$ref"]
    assert role_ref.startswith("#/$defs/")
    assert resolved["schema"]["$defs"][role_ref.split("#/$defs/", 1)[1]]["type"] == "object"


def test_v1_objecttypes_registers_generic_custom_nodeid_type_as_known(client: TestClient) -> None:
    app = fastapi_app(client)
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
    assert resolved["elementId"].startswith("urn:") and ":objecttype:" in resolved["elementId"]
    assert resolved["displayName"] == "DataType"
    assert isinstance(resolved["schema"].get("oneOf"), list)


def test_v1_objecttypes_unresolved_standard_property_datatype_gets_fallback_schema(client: TestClient) -> None:
    app = fastapi_app(client)
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
    assert resolved["elementId"].startswith("urn:") and ":objecttype:" in resolved["elementId"]
    assert not resolved["displayName"].startswith("InferredType_")
    assert isinstance(resolved["schema"].get("oneOf"), list)
    assert resolved["schema"]["x-opcua-nodeId"] == "nsu=http://opcfoundation.org/UA/;i=14119"


def _add_unresolved_custom_object_type(
    app: FastAPI,
    node_id: str,
    name: str,
    source_node_id: str,
    numeric_id: int,
) -> str:
    """Attach an asset whose ``sourceTypeId`` has no upstream object type definition.

    ``nsu=http://example.com/custom;i=<numeric_id>`` is therefore referenced but unresolved:
    it is not a property datatype, so ``/v1/objecttypes`` infers a generic placeholder and may
    resolve the display name through the live ``read_browse_name`` lookup.
    """
    source_type_id = f"nsu=http://example.com/custom;i={numeric_id}"
    model = app.state.model_cache
    model.nodes_by_id[node_id] = ModelNode(
        id=node_id,
        name=name,
        kind="asset",
        type=None,
        children=[],
        source_node_id=source_node_id,
        source_type_id=source_type_id,
        parent_id="asset-root",
    )
    model.children_by_id[node_id] = []
    model.children_by_id.setdefault("asset-root", []).append(node_id)
    model.hierarchy_children_by_id.setdefault("asset-root", []).append(node_id)
    model.instances_by_type_id[source_type_id] = [node_id]
    return source_type_id


def test_v1_objecttypes_generic_custom_id_uses_browse_name_lookup(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live BrowseName lookup must be patched at its source module (docs/architecture.md:178).

    ``nsu=http://example.com/custom;i=900001`` is an unresolved *object* type (no datatype
    schema, no registry name), so the generic inference path runs and the inferred name is
    replaced by the ``read_browse_name`` result.
    """
    monkeypatch.setattr("i3x_server.api.v1.objecttype_helpers._ENABLE_LIVE_TYPE_NAME_LOOKUP", True)
    monkeypatch.setattr("i3x_server.api.v1.objecttype_helpers._LIVE_TYPE_NAME_LOOKUP_MAX_PER_REQUEST", 100)

    app = fastapi_app(client)
    _add_unresolved_custom_object_type(app, "asset-custom-900001", "CustomAsset", "ns=2;s=CustomAsset", 900001)

    response = client.get("/v1/objecttypes")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True

    resolved = next(
        (item for item in payload["result"] if item["sourceTypeId"] == "nsu=http://example.com/custom;i=900001"),
        None,
    )
    assert resolved is not None
    assert resolved["displayName"] == "CustomNetworkAddressType"
    assert resolved["schema"]["title"] == "CustomNetworkAddressType"


def test_v1_objecttypes_lookup_budget_limits_browse_name_reads(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-request budget caps how many inferred type ids may be looked up."""
    monkeypatch.setattr("i3x_server.api.v1.objecttype_helpers._ENABLE_LIVE_TYPE_NAME_LOOKUP", True)
    monkeypatch.setattr("i3x_server.api.v1.objecttype_helpers._LIVE_TYPE_NAME_LOOKUP_MAX_PER_REQUEST", 1)

    app = fastapi_app(client)
    _add_unresolved_custom_object_type(app, "asset-custom-900001", "CustomAsset", "ns=2;s=CustomAsset", 900001)
    _add_unresolved_custom_object_type(app, "asset-custom-900002", "MachineState", "ns=2;s=MachineState", 900002)

    response = client.get("/v1/objecttypes")
    assert response.status_code == 200
    by_source = {item["sourceTypeId"]: item["displayName"] for item in response.json()["result"]}
    assert by_source["nsu=http://example.com/custom;i=900001"] == "CustomNetworkAddressType"
    assert by_source["nsu=http://example.com/custom;i=900002"].startswith("InferredType_")


async def test_v1_objecttypes_standard_namespace_lookup_remaps_to_ns0_index(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standard-UA inferred ids are looked up with ``ns=0;i=…``, not the expanded ``nsu=`` form."""
    monkeypatch.setattr("i3x_server.api.v1.objecttype_helpers._ENABLE_LIVE_TYPE_NAME_LOOKUP", True)
    monkeypatch.setattr("i3x_server.api.v1.objecttype_helpers._LIVE_TYPE_NAME_LOOKUP_MAX_PER_REQUEST", 100)

    app = fastapi_app(client)
    looked_up: list[str] = []

    async def browse_name(node_id: str) -> str | None:
        looked_up.append(node_id)
        return "VendorDiagnosticsType" if node_id == "ns=0;i=999999" else None

    app.state.opcua_client.read_browse_name = browse_name
    model = app.state.model_cache
    model.nodes_by_id["asset-ua-999999"] = ModelNode(
        id="asset-ua-999999",
        name="Diagnostics",
        kind="asset",
        type=None,
        children=[],
        source_node_id="ns=2;s=Diagnostics",
        source_type_id="nsu=http://opcfoundation.org/UA/;i=999999",
        parent_id="asset-root",
    )
    model.children_by_id["asset-ua-999999"] = []
    model.children_by_id.setdefault("asset-root", []).append("asset-ua-999999")
    model.hierarchy_children_by_id.setdefault("asset-root", []).append("asset-ua-999999")
    model.instances_by_type_id["nsu=http://opcfoundation.org/UA/;i=999999"] = ["asset-ua-999999"]

    response = client.get("/v1/objecttypes")
    assert response.status_code == 200
    resolved = next(
        (
            item
            for item in response.json()["result"]
            if item["sourceTypeId"] == "nsu=http://opcfoundation.org/UA/;i=999999"
        ),
        None,
    )
    assert resolved is not None
    assert looked_up == ["ns=0;i=999999"]
    assert resolved["displayName"] == "VendorDiagnosticsType"


@pytest.mark.asyncio
async def test_v1_objecttypes_lookup_timeout_falls_back_to_inferred_name(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow upstream BrowseName read must degrade to the inferred name, not hang the request."""
    monkeypatch.setattr("i3x_server.api.v1.objecttype_helpers._ENABLE_LIVE_TYPE_NAME_LOOKUP", True)
    monkeypatch.setattr("i3x_server.api.v1.objecttype_helpers._LIVE_TYPE_NAME_LOOKUP_MAX_PER_REQUEST", 100)
    monkeypatch.setattr("i3x_server.api.v1.objecttype_helpers._live_type_name_lookup_timeout_seconds", lambda: 0.05)

    app = fastapi_app(client)

    async def slow_browse_name(node_id: str) -> str | None:
        del node_id
        await asyncio.sleep(0.5)
        return "CustomNetworkAddressType"

    app.state.opcua_client.read_browse_name = slow_browse_name
    _add_unresolved_custom_object_type(app, "asset-custom-900001", "CustomAsset", "ns=2;s=CustomAsset", 900001)

    response = client.get("/v1/objecttypes")
    assert response.status_code == 200
    resolved = next(
        (
            item
            for item in response.json()["result"]
            if item["sourceTypeId"] == "nsu=http://example.com/custom;i=900001"
        ),
        None,
    )
    assert resolved is not None
    assert resolved["displayName"].startswith("InferredType_")


def test_v1_objecttypes_does_not_publish_null_opcua_type_id(client: TestClient) -> None:
    app = fastapi_app(client)
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
