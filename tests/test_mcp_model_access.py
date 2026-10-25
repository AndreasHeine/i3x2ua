from __future__ import annotations

from typing import Any, NoReturn
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from starlette.requests import Request

from i3x_server.api.mcp.monolithic import router
from i3x_server.api.v1.contracts import ObjectTypeResponse
from i3x_server.api.v1.objecttype_helpers import _object_type_context_revision, _ObjectTypeContext
from i3x_server.application.ports.opcua import OpcUaNamespaceInfo, OpcUaObjectTypeInfo
from i3x_server.mcp import build_mcp_tools, invoke_mcp_tool
from i3x_server.mcp.model_access import MCP_MODEL_ACCESS_SCHEMAS, invoke_model_access_tool
from i3x_server.mcp.model_tools import _validate_model_tool_arguments
from i3x_server.model.revision import model_revision
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult

_ACCESS_ARGUMENTS: dict[str, dict[str, Any]] = {
    "resolveBrowsePath": {"path": "Machines/Station/Temperature"},
    "getBrowsePath": {"elementId": "temp"},
    "getInstancesOfType": {"typeElementId": "type-base"},
    "getChildren": {"elementId": "root"},
    "getCompositionProperties": {"elementIds": ["station-a"]},
    "findElementsByName": {"text": "Temperature"},
    "findElementsByNamespace": {"namespaceUri": "urn:machines"},
    "findElementsByRole": {"semanticRole": "datapoint"},
}


class _ForbiddenLiveAccess:
    def __getattr__(self, name: str) -> NoReturn:
        raise AssertionError(f"Cached model tools must not access live dependency: {name}")


@pytest.fixture
def model() -> BuildResult:
    nodes = [
        ModelNode(id="root", name="Machines", kind="asset", source_node_id="root"),
        ModelNode(id="station-b", name="Station", kind="asset", source_node_id="b", source_type_id="ns=2;i=2"),
        ModelNode(id="station-a", name="Station", kind="asset", source_node_id="a", source_type_id="ns=2;i=1"),
        ModelNode(id="component", name="Motor", kind="asset", source_node_id="c", type="type-grandchild"),
        ModelNode(id="temp", name="Temperature", kind="property", source_node_id="t", source_type_id="ns=2;i=1"),
        ModelNode(id="temp2", name="TemperatureSetpoint", kind="property", source_node_id="t2"),
        ModelNode(id="slash", name="Pressure/Flow", kind="property", source_node_id="s"),
        ModelNode(id="action", name="Start", kind="action", source_node_id="m", source_type_id="ns=2;i=1"),
        ModelNode(id="other", name="Other", kind="asset", source_node_id="o"),
    ]
    return BuildResult(
        nodes_by_id={node.id: node for node in nodes},
        root_ids=["root", "other"],
        children_by_id={},
        instances_by_type_id={"ns=2;i=1": ["station-a", "station-a", "temp", "missing"]},
        property_to_node={},
        action_to_method={},
        hierarchy_parent_by_id={"station-a": "root", "station-b": "root"},
        parent_id_by_id={"component": "station-a", "temp": "station-a"},
        composition_parent_by_id={"slash": "station-a", "temp2": "component"},
        hierarchy_children_by_id={"root": ["station-b", "station-a", "station-a", "missing"]},
        composition_children_by_id={
            "station-a": ["temp", "component", "slash", "action"],
            "component": ["temp2", "station-a"],
        },
        graph_related_by_id={"station-a": [("Links", "station-b"), ("Other", "station-b"), ("Links", "missing")]},
        namespace_uri_by_id={"station-a": "urn:machines", "temp": "urn:machines"},
        semantic_role_by_id={"station-a": "asset", "station-b": "asset", "temp": "datapoint"},
    )


@pytest.fixture
def context() -> _ObjectTypeContext:
    infos = [
        OpcUaObjectTypeInfo("ns=2;i=1", None, "Base", "Base", {}),
        OpcUaObjectTypeInfo("ns=2;i=2", "ns=2;i=1", "Child", "Child", {}),
        OpcUaObjectTypeInfo("ns=2;i=3", "ns=2;i=2", "Grandchild", "Grandchild", {}),
        OpcUaObjectTypeInfo("ns=2;i=4", None, "Empty", "Empty", {}),
    ]
    ids = ["type-base", "type-child", "type-grandchild", "type-empty"]
    return _ObjectTypeContext(
        namespace_infos=[],
        object_types=infos,
        element_ids_by_node_id={info.node_id: element_id for info, element_id in zip(infos, ids, strict=True)},
        items=[
            ObjectTypeResponse(
                elementId=element_id,
                displayName=info.display_name,
                namespaceUri="urn:types",
                sourceTypeId=f"nsu=urn:types;i={index}",
                schema={},
            )
            for index, (info, element_id) in enumerate(zip(infos, ids, strict=True), 1)
        ],
        source_type_to_element_id={},
        revision="type-context-v1",
    )


def test_path_resolution_and_canonical_paths(model: BuildResult) -> None:
    assert invoke_model_access_tool(model, "resolveBrowsePath", {"path": "Machines/Station/Temperature"}) == {
        "modelRevision": model_revision(model),
        "found": True,
        "elementId": "temp",
        "displayName": "Temperature",
        "kind": "property",
    }
    assert (
        invoke_model_access_tool(model, "resolveBrowsePath", {"segments": ["Machines", "Station", "Pressure/Flow"]})[
            "elementId"
        ]
        == "slash"
    )
    assert (
        invoke_model_access_tool(model, "resolveBrowsePath", {"path": "machines/Station/Temperature"})["found"] is False
    )
    assert invoke_model_access_tool(model, "resolveBrowsePath", {"path": "Machines"})["elementId"] == "root"
    assert invoke_model_access_tool(model, "getBrowsePath", {"elementId": "temp2"}) == {
        "modelRevision": model_revision(model),
        "path": "Machines/Station/Motor/TemperatureSetpoint",
        "segments": ["Machines", "Station", "Motor", "TemperatureSetpoint"],
    }
    assert invoke_model_access_tool(model, "getBrowsePath", {"elementId": "root"})["segments"] == ["Machines"]
    with pytest.raises(HTTPException) as exc:
        invoke_model_access_tool(model, "resolveBrowsePath", {"path": "Machines/Station"})
    assert exc.value.status_code == 409


@pytest.mark.parametrize("parent", ["temp", "missing"])
def test_invalid_parent_chains_fail_explicitly(model: BuildResult, parent: str) -> None:
    model.parent_id_by_id["temp"] = parent
    with pytest.raises(HTTPException) as exc:
        invoke_model_access_tool(model, "getBrowsePath", {"elementId": "temp"})
    assert exc.value.status_code == 500
    with pytest.raises(HTTPException) as exc:
        invoke_model_access_tool(model, "resolveBrowsePath", {"path": "Machines"})
    assert exc.value.status_code == 500


def test_paths_support_deep_models_without_recursion(model: BuildResult) -> None:
    for index in range(1100):
        element_id = f"deep-{index}"
        model.nodes_by_id[element_id] = ModelNode(
            id=element_id, name=str(index), kind="asset", source_node_id=element_id
        )
        model.parent_id_by_id[element_id] = f"deep-{index - 1}" if index else "root"
    result = invoke_model_access_tool(model, "getBrowsePath", {"elementId": "deep-1099"})
    assert len(result["segments"]) == 1101
    assert (
        invoke_model_access_tool(model, "resolveBrowsePath", {"segments": result["segments"]})["elementId"]
        == "deep-1099"
    )
    access_index = model._model_access_index_cache
    assert access_index is not None
    path_children = access_index.path_children
    assert path_children is not None
    assert sum(len(children) for by_name in path_children.values() for children in by_name.values()) == len(
        model.nodes_by_id
    )


def test_type_instances_are_deduplicated_sorted_and_subtype_aware(
    model: BuildResult, context: _ObjectTypeContext
) -> None:
    result = invoke_model_access_tool(model, "getInstancesOfType", {"typeElementId": "type-base", "limit": 2}, context)
    assert result == {
        "modelRevision": model_revision(model),
        "typeContextRevision": context.revision,
        "total": 3,
        "returned": 2,
        "offset": 0,
        "nextOffset": 2,
        "items": [["component", "Motor"], ["station-a", "Station"]],
    }
    direct = invoke_model_access_tool(
        model, "getInstancesOfType", {"typeElementId": "type-base", "includeSubtypes": False}, context
    )
    assert direct["items"] == [["station-a", "Station"]]
    assert invoke_model_access_tool(
        model,
        "getInstancesOfType",
        {"typeElementId": "type-base", "offset": 2, "typeContextRevision": context.revision},
        context,
    )["items"] == [["station-b", "Station"]]
    empty = invoke_model_access_tool(model, "getInstancesOfType", {"typeElementId": "type-empty"}, context)
    assert empty["items"] == [] and empty["total"] == 0
    with pytest.raises(HTTPException) as exc:
        invoke_model_access_tool(model, "getInstancesOfType", {"typeElementId": "unknown"}, context)
    assert exc.value.status_code == 404


def test_type_cycle_fails_and_context_replacement_rebuilds_indexes(
    model: BuildResult, context: _ObjectTypeContext
) -> None:
    invoke_model_access_tool(model, "getInstancesOfType", {"typeElementId": "type-base"}, context)
    replacement = _ObjectTypeContext(
        namespace_infos=[],
        object_types=[],
        element_ids_by_node_id={},
        items=context.items,
        source_type_to_element_id={},
    )
    assert (
        invoke_model_access_tool(model, "getInstancesOfType", {"typeElementId": "type-base"}, replacement)["items"]
        == []
    )
    context.object_types[0].parent_node_id = "ns=2;i=3"
    with pytest.raises(HTTPException) as exc:
        invoke_model_access_tool(model, "getInstancesOfType", {"typeElementId": "type-base"}, context)
    assert exc.value.status_code == 500


def test_type_instance_pagination_rejects_changed_context(model: BuildResult, context: _ObjectTypeContext) -> None:
    first_page = invoke_model_access_tool(
        model, "getInstancesOfType", {"typeElementId": "type-base", "limit": 1}, context
    )
    replacement = _ObjectTypeContext(
        namespace_infos=context.namespace_infos,
        object_types=context.object_types,
        element_ids_by_node_id=context.element_ids_by_node_id,
        items=context.items,
        source_type_to_element_id=context.source_type_to_element_id,
        revision="type-context-v2",
    )
    with pytest.raises(HTTPException) as exc:
        invoke_model_access_tool(
            model,
            "getInstancesOfType",
            {
                "typeElementId": "type-base",
                "offset": 1,
                "typeContextRevision": first_page["typeContextRevision"],
            },
            replacement,
        )
    assert exc.value.status_code == 409


def test_type_context_revision_covers_ancestry_and_aliases(context: _ObjectTypeContext) -> None:
    original = _object_type_context_revision(context)
    context.object_types[1].parent_node_id = "ns=2;i=3"
    changed_ancestry = _object_type_context_revision(context)
    context.source_type_to_element_id["nsu=urn:types;i=999"] = "type-base"
    changed_aliases = _object_type_context_revision(context)

    assert changed_ancestry != original
    assert changed_aliases != changed_ancestry


def test_type_aliases_and_index_only_instances(model: BuildResult, context: _ObjectTypeContext) -> None:
    context.source_type_to_element_id["nsu=urn:types;s=base"] = "type-base"
    model.nodes_by_id["other"].source_type_id = "nsu=urn:types;s=base"
    model.nodes_by_id["station-a"].source_type_id = None
    model.nodes_by_id["component"].source_type_id = "unmapped-source"
    model.nodes_by_id["station-b"].source_type_id = "nsu=urn:types;i=2"
    result = invoke_model_access_tool(model, "getInstancesOfType", {"typeElementId": "type-base"}, context)
    assert result["items"] == [
        ["component", "Motor"],
        ["other", "Other"],
        ["station-a", "Station"],
        ["station-b", "Station"],
    ]


def test_inferred_type_instance_lookup_normalizes_indexed_source_id(
    model: BuildResult, context: _ObjectTypeContext
) -> None:
    inferred_item = ObjectTypeResponse(
        elementId="type-inferred",
        displayName="Inferred",
        namespaceUri="urn:vendor",
        sourceTypeId="nsu=urn:vendor;i=9001",
        schema={},
    )
    context.namespace_infos = [
        OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="UA"),
        OpcUaNamespaceInfo(uri="urn:unused", display_name="Unused"),
        OpcUaNamespaceInfo(uri="urn:vendor", display_name="Vendor"),
    ]
    context.items.append(inferred_item)
    context.source_type_to_element_id[inferred_item.sourceTypeId] = inferred_item.elementId
    inferred_instance = ModelNode(
        id="inferred-instance",
        name="Inferred instance",
        kind="asset",
        source_node_id="inferred-instance-source",
        source_type_id="ns=2;i=9001",
    )
    model.nodes_by_id[inferred_instance.id] = inferred_instance
    model.instances_by_type_id["ns=2;i=9001"] = [inferred_instance.id]
    model.revision = ""

    result = invoke_model_access_tool(model, "getInstancesOfType", {"typeElementId": "type-inferred"}, context)

    assert result["items"] == [["inferred-instance", "Inferred instance"]]


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"elementId": "root"}, [["station-a", "Station"], ["station-b", "Station"]]),
        ({"elementId": "station-a", "plane": "graph"}, [["station-b", "Station"]]),
        ({"elementId": "station-a", "plane": "composition", "limit": 1}, [["component", "Motor"]]),
        ({"elementId": "station-a", "plane": "hierarchy"}, []),
    ],
)
def test_children_keep_planes_separate(
    model: BuildResult, arguments: dict[str, Any], expected: list[list[str]]
) -> None:
    result = invoke_model_access_tool(model, "getChildren", arguments)
    assert result["items"] == expected
    assert result["modelRevision"] == model_revision(model)


def test_composition_properties_batch_has_independent_pages_and_ignores_hierarchy(model: BuildResult) -> None:
    result = invoke_model_access_tool(
        model, "getCompositionProperties", {"elementIds": ["station-a", "root", "station-a"], "offset": 1, "limit": 1}
    )
    assert result["items"] == [
        {"elementId": "root", "total": 0, "returned": 0, "offset": 1, "properties": []},
        {
            "elementId": "station-a",
            "total": 3,
            "returned": 1,
            "offset": 1,
            "nextOffset": 2,
            "properties": [["temp", "Temperature"]],
        },
    ]
    with pytest.raises(HTTPException) as exc:
        invoke_model_access_tool(model, "getCompositionProperties", {"elementIds": ["temp"]})
    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("findElementsByName", {"text": "TEMPERATURE"}, [["temp", "Temperature"], ["temp2", "TemperatureSetpoint"]]),
        ("findElementsByName", {"text": "t", "offset": 2, "limit": 1}, [["action", "Start"]]),
        ("findElementsByName", {"text": "emperatur"}, [["temp", "Temperature"], ["temp2", "TemperatureSetpoint"]]),
        ("findElementsByName", {"text": "ns=2;i=1"}, []),
        ("findElementsByName", {"text": "not-present"}, []),
        (
            "findElementsByNamespace",
            {"namespaceUri": "urn:machines"},
            [["station-a", "Station"], ["temp", "Temperature"]],
        ),
        ("findElementsByNamespace", {"namespaceUri": "urn:missing"}, []),
        ("findElementsByRole", {"semanticRole": "asset"}, [["station-a", "Station"], ["station-b", "Station"]]),
        ("findElementsByRole", {"semanticRole": "datapoint", "offset": 10}, []),
    ],
)
def test_compact_indexed_searches(
    model: BuildResult, name: str, arguments: dict[str, Any], expected: list[list[str]]
) -> None:
    result = invoke_model_access_tool(model, name, arguments)
    assert result["items"] == expected
    assert result["returned"] == len(expected)
    assert result["modelRevision"] == model_revision(model)


def test_indexes_reused_and_invalidated_by_revision(model: BuildResult) -> None:
    invoke_model_access_tool(model, "resolveBrowsePath", {"path": "Other"})
    cached = model._model_access_index_cache
    invoke_model_access_tool(model, "findElementsByRole", {"semanticRole": "asset"})
    assert model._model_access_index_cache is cached
    model.nodes_by_id["other"].name = "Renamed"
    model.revision = ""
    assert invoke_model_access_tool(model, "resolveBrowsePath", {"path": "Other"})["found"] is False
    assert invoke_model_access_tool(model, "resolveBrowsePath", {"path": "Renamed"})["elementId"] == "other"
    assert model._model_access_index_cache is not cached
    replacement = BuildResult(
        nodes_by_id={},
        root_ids=[],
        children_by_id={},
        instances_by_type_id={},
        property_to_node={},
        action_to_method={},
    )
    assert invoke_model_access_tool(replacement, "findElementsByName", {"text": "Renamed"})["total"] == 0


def test_metadata_fallbacks_and_hierarchy_parent_precedence(model: BuildResult) -> None:
    model.nodes_by_id["other"].metadata["namespaceUri"] = "urn:fallback"
    model.nodes_by_id["other"].semantic_role = "component"
    model.nodes_by_id["other"].parent_id = "root"
    model.hierarchy_parent_by_id["temp"] = "other"
    assert invoke_model_access_tool(model, "findElementsByNamespace", {"namespaceUri": "urn:fallback"})["items"] == [
        ["other", "Other"]
    ]
    assert invoke_model_access_tool(model, "findElementsByRole", {"semanticRole": "component"})["items"] == [
        ["other", "Other"]
    ]
    assert (
        invoke_model_access_tool(model, "getBrowsePath", {"elementId": "temp"})["path"] == "Machines/Other/Temperature"
    )


def test_path_and_type_lookups_reuse_indexes_without_scanning_nodes(
    model: BuildResult, context: _ObjectTypeContext
) -> None:
    class NoNodeScan(dict[str, ModelNode]):
        def __iter__(self) -> NoReturn:
            pytest.fail("Indexed lookup must not iterate the model")

        def values(self) -> NoReturn:
            pytest.fail("Indexed lookup must not scan model values")

    invoke_model_access_tool(model, "resolveBrowsePath", {"path": "Other"})
    invoke_model_access_tool(model, "getInstancesOfType", {"typeElementId": "type-base"}, context)
    model.nodes_by_id = NoNodeScan(model.nodes_by_id)
    assert invoke_model_access_tool(model, "resolveBrowsePath", {"path": "Other"})["elementId"] == "other"
    assert invoke_model_access_tool(model, "getInstancesOfType", {"typeElementId": "type-base"}, context)["total"] == 3
    assert invoke_model_access_tool(model, "findElementsByNamespace", {"namespaceUri": "urn:machines"})["total"] == 2
    assert invoke_model_access_tool(model, "findElementsByRole", {"semanticRole": "asset"})["total"] == 2


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("resolveBrowsePath", {}),
        ("resolveBrowsePath", {"path": "a", "segments": ["a"]}),
        ("resolveBrowsePath", {"path": "/a"}),
        ("resolveBrowsePath", {"path": "a//b"}),
        ("resolveBrowsePath", {"segments": ["a", " "]}),
        ("resolveBrowsePath", {"segments": []}),
        ("getBrowsePath", {"elementId": " "}),
        ("getChildren", {"elementId": "root", "plane": "invalid"}),
        ("getChildren", {"elementId": "root", "limit": True}),
        ("getChildren", {"elementId": "root", "limit": 1.0}),
        ("getChildren", {"elementId": "root", "offset": -1}),
        ("getChildren", {"elementId": "root", "limit": 101}),
        ("getChildren", {"elementId": "root", "unexpected": True}),
        ("getInstancesOfType", {"typeElementId": "type-base", "offset": 1}),
        ("getCompositionProperties", {"elementIds": []}),
        ("getCompositionProperties", {"elementIds": ["root"] * 51}),
        ("getCompositionProperties", {"elementIds": [False]}),
        ("getInstancesOfType", {"typeElementId": "type-base", "includeSubtypes": 1}),
        ("searchModelElements", {}),
        ("searchModelElements", {"text": ""}),
        ("searchModelElements", {"text": "  ", "kind": "asset"}),
        ("findElementsByName", {"text": ""}),
        ("findElementsByNamespace", {"namespaceUri": 2}),
        ("findElementsByRole", {"semanticRole": "wrong"}),
    ],
)
def test_access_argument_validation(name: str, arguments: dict[str, Any]) -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_model_tool_arguments(name, arguments)
    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("getBrowsePath", {"elementId": "missing"}),
        ("getChildren", {"elementId": "missing"}),
        ("getCompositionProperties", {"elementIds": ["root", "missing"]}),
    ],
)
def test_unknown_elements_fail_explicitly(model: BuildResult, name: str, arguments: dict[str, Any]) -> None:
    with pytest.raises(HTTPException) as exc:
        invoke_model_access_tool(model, name, arguments)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_all_access_tools_are_registered_read_only_and_never_build_or_contact_ua(
    model: BuildResult, context: _ObjectTypeContext
) -> None:
    app = FastAPI()
    app.state.model_cache = model
    app.state.object_type_context_cache = {"model_token": model_revision(model), "context": context}
    forbidden = AsyncMock(side_effect=AssertionError("Cached model tools must not contact OPC UA or build a model"))
    app.state.mcp_model_provider = forbidden
    app.state.model_builder = _ForbiddenLiveAccess()
    app.state.opcua_client = _ForbiddenLiveAccess()
    request = Request({"type": "http", "app": app, "headers": []})
    tools = build_mcp_tools({"paths": {}})
    for name, schema in MCP_MODEL_ACCESS_SCHEMAS.items():
        Draft202012Validator.check_schema(schema)
        assert tools[name].input_schema == schema
        assert tools[name].annotations["readOnlyHint"] is True
        assert tools[name].annotations["destructiveHint"] is False
        assert tools[name].annotations["openWorldHint"] is False
        response = await invoke_mcp_tool(request, tools[name], _ACCESS_ARGUMENTS[name])
        assert response["status_code"] == 200
        assert response["body"]["modelRevision"] == model_revision(model)
    forbidden.assert_not_called()


@pytest.mark.asyncio
async def test_missing_or_stale_cached_context_never_refreshes(model: BuildResult, context: _ObjectTypeContext) -> None:
    app = FastAPI()
    app.state.mcp_model_provider = AsyncMock(side_effect=AssertionError("Must not build a model"))
    request = Request({"type": "http", "app": app, "headers": []})
    tools = build_mcp_tools({"paths": {}})
    for name, arguments in _ACCESS_ARGUMENTS.items():
        with pytest.raises(HTTPException) as exc:
            await invoke_mcp_tool(request, tools[name], arguments)
        assert exc.value.status_code == 503
    app.state.model_cache = model
    for cache in (None, {"model_token": "old", "context": context}, {"model_token": model_revision(model)}):
        app.state.object_type_context_cache = cache
        with pytest.raises(HTTPException) as exc:
            await invoke_mcp_tool(request, tools["getInstancesOfType"], {"typeElementId": "type-base"})
        assert exc.value.status_code == 503
    app.state.mcp_model_provider.assert_not_called()


def test_model_access_json_rpc_preserves_compact_payloads(model: BuildResult, context: _ObjectTypeContext) -> None:
    app = FastAPI()
    app.include_router(router)
    app.state.mcp_tools = build_mcp_tools({"paths": {}})
    app.state.model_cache = model
    app.state.object_type_context_cache = {"model_token": model_revision(model), "context": context}
    app.state.opcua_client = _ForbiddenLiveAccess()
    app.state.model_builder = _ForbiddenLiveAccess()
    with TestClient(app) as client:
        listed = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
        assert set(_ACCESS_ARGUMENTS).issubset({tool["name"] for tool in listed["result"]["tools"]})
        for name, arguments in _ACCESS_ARGUMENTS.items():
            response = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
            result = response.json()["result"]
            assert result["isError"] is False
            body = result["structuredContent"]["body"]
            assert body == invoke_model_access_tool(model, name, arguments, context)
            assert body["modelRevision"] == model_revision(model)
