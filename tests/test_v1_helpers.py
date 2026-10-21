from __future__ import annotations

import asyncio
import gc
import json
import weakref
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException
from jsonschema import Draft202012Validator

import i3x_server.api.v1.monolithic as v1
from i3x_server.api.v1 import object_helpers, objecttype_helpers
from i3x_server.application.services.model_query import _relationship_type_items
from i3x_server.infrastructure.opcua.client import (
    OpcUaClientProtocol,
    OpcUaNamespaceInfo,
    OpcUaObjectTypeInfo,
    OpcUaObjectTypeMemberInfo,
)
from i3x_server.model.revision import model_revision
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult


def test_relationship_type_helpers_use_model_namespaces() -> None:
    model = _sample_model()
    model.graph_relationship_names = {"ConnectedTo"}
    model.graph_relationship_namespace_by_id = {"ConnectedTo": "urn:example:custom"}
    expected = _relationship_type_items(model)
    for helper in (v1._relationship_type_items, object_helpers._relationship_type_items):
        assert [item.model_dump() for item in helper(model)] == expected


def test_object_instance_metadata_uses_node_description_or_fallback() -> None:
    model = _sample_model()
    node = model.nodes_by_id["asset-root"]

    for helper in (v1._build_object_instance_metadata, object_helpers._build_object_instance_metadata):
        node.metadata["description"] = " OPC UA description "
        metadata = helper(model, node, None, "nsu=urn:example;s=Root")
        assert metadata is not None
        assert metadata.description == " OPC UA description "

        node.metadata["description"] = " "
        metadata = helper(model, node, None, "nsu=urn:example;s=Root")
        assert metadata is not None
        assert metadata.description == "Derived from model node Root"


def _sample_model() -> BuildResult:
    root = ModelNode(
        id="asset-root",
        name="Root",
        kind="asset",
        type=None,
        children=["asset-child", "prop-a"],
        source_node_id="ns=2;s=Root",
    )
    child_asset = ModelNode(
        id="asset-child",
        name="Child",
        kind="asset",
        type=None,
        children=["prop-b"],
        source_node_id="ns=2;s=Child",
    )
    prop_a = ModelNode(
        id="prop-a",
        name="Temperature",
        kind="property",
        type="ns=1;i=11",
        children=[],
        source_node_id="ns=2;s=Temperature",
    )
    prop_b = ModelNode(
        id="prop-b",
        name="Pressure",
        kind="property",
        type="ns=1;i=11",
        children=[],
        source_node_id="ns=2;s=Pressure",
    )
    return BuildResult(
        nodes_by_id={
            root.id: root,
            child_asset.id: child_asset,
            prop_a.id: prop_a,
            prop_b.id: prop_b,
        },
        root_ids=[root.id],
        children_by_id={
            root.id: [child_asset.id, prop_a.id],
            child_asset.id: [prop_b.id],
            prop_a.id: [],
            prop_b.id: [],
        },
        instances_by_type_id={},
        property_to_node={"prop-a": "ns=2;s=Temperature", "prop-b": "ns=2;s=Pressure"},
        action_to_method={},
        hierarchy_children_by_id={
            root.id: [child_asset.id],
            child_asset.id: [],
            prop_a.id: [],
            prop_b.id: [],
        },
        composition_children_by_id={
            root.id: [prop_a.id, child_asset.id],
            child_asset.id: [prop_b.id],
            prop_a.id: [],
            prop_b.id: [],
        },
        hierarchy_parent_by_id={child_asset.id: root.id},
        composition_parent_by_id={prop_a.id: root.id, child_asset.id: root.id, prop_b.id: child_asset.id},
    )


def test_display_name_for_uri_variants() -> None:
    assert v1._display_name_for_uri("https://cesmii.org/i3x") == "I3X"
    assert v1._display_name_for_uri("https://example.com/ns/device_model") == "Device Model"
    assert v1._display_name_for_uri("https://example.com/ns/a12") == "A12"


def test_namespace_and_expanded_node_helpers() -> None:
    infos = [
        OpcUaNamespaceInfo(uri="http://default", display_name="Default"),
        OpcUaNamespaceInfo(uri="http://custom", display_name="Custom"),
    ]
    assert v1._namespace_uri_for_node_id("ns=1;i=42", infos) == "http://custom"
    assert v1._namespace_uri_for_node_id("ns=99;i=42", infos) == ""
    assert v1._expanded_node_id("ns=1;i=42", infos) == "nsu=http://custom;i=42"
    assert v1._expanded_node_id("nsu=http://custom;i=42", infos) == "nsu=http://custom;i=42"
    assert v1._expanded_node_id("asset-root", infos) == "asset-root"
    assert v1._namespace_uri_from_expanded_node_id("nsu=http://custom;i=42") == "http://custom"
    assert v1._namespace_uri_from_expanded_node_id("ns=1;i=42") is None


def test_namespace_canonicalization_helper() -> None:
    infos = [
        OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="UA"),
        OpcUaNamespaceInfo(uri="http://example.com/custom/", display_name="Custom"),
    ]
    assert v1._canonical_namespace_uri("http://opcfoundation.org/UA", infos) == "http://opcfoundation.org/UA/"
    assert v1._canonical_namespace_uri("http://example.com/custom", infos) == "http://example.com/custom/"
    assert v1._canonical_namespace_uri("http://not-declared", infos) == "http://not-declared"


def test_unknown_type_placeholder_uses_declared_namespace_when_unresolved() -> None:
    infos = [
        OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="UA"),
        OpcUaNamespaceInfo(uri="http://example.com/custom", display_name="Custom"),
    ]
    placeholder = v1._unknown_type_placeholder("unknown-type", infos)
    assert placeholder.namespaceUri == "http://opcfoundation.org/UA/"


def test_element_and_urn_token_helpers() -> None:
    assert v1._to_element_id("MachineType") == "machine-type"
    assert v1._to_element_id("__") == "unknown-type"
    assert v1._to_urn_token("HTTP://Example.COM/Plant 01") == "http-example-com-plant-01"


def test_builtin_ua_datatype_helper_detection() -> None:
    assert v1._is_builtin_ua_datatype_node_id("nsu=http://opcfoundation.org/UA/;i=12") is True
    assert v1._is_builtin_ua_datatype_node_id("nsu=http://opcfoundation.org/UA/;i=11492") is False


def test_unconstrained_datatype_wrapper_accepts_null_and_arrays_without_overlapping_branches() -> None:
    schema = objecttype_helpers._wrapped_value_schema(
        {},
        display_name="Unknown",
        source_type_id="nsu=urn:custom;i=999999",
    )
    assert "oneOf" not in schema
    validator = Draft202012Validator(schema)
    values: list[Any] = [None, True, 42, 1.5, "text", [], {}, [None, 42]]
    for value in values:
        validator.validate(value)


def test_unresolved_builtin_datatype_fallback_is_unconstrained(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(objecttype_helpers, "build_data_type_schema", lambda *_args: None)
    response = objecttype_helpers._datatype_object_type_from_source_type_id(
        "nsu=http://opcfoundation.org/UA/;i=24",
        [OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="UA")],
    )
    assert response is not None
    schema = response.model_dump(by_alias=True)["schema"]
    assert "oneOf" not in schema
    validator = Draft202012Validator(schema)
    values: list[Any] = [None, True, 42, 1.5, "text", [], {}, [None, 42]]
    for value in values:
        validator.validate(value)


@pytest.mark.parametrize(
    ("builtin_node_id", "expected"),
    [
        ("ns=0;i=3", {"type": "integer", "minimum": 0, "maximum": 255}),
        ("ns=0;i=5", {"type": "integer", "minimum": 0, "maximum": 65535}),
    ],
)
def test_custom_scalar_datatype_uses_nearest_concrete_builtin_ancestor(
    builtin_node_id: str,
    expected: dict[str, Any],
) -> None:
    hierarchy = [
        SimpleNamespace(node_id="ns=2;i=3001", browse_name="VendorWord"),
        SimpleNamespace(node_id="ns=0;i=28", browse_name="UInteger"),
        SimpleNamespace(node_id=builtin_node_id, browse_name="Byte" if builtin_node_id == "ns=0;i=3" else "UInt16"),
        SimpleNamespace(node_id="ns=0;i=27", browse_name="Integer"),
        SimpleNamespace(node_id="ns=0;i=26", browse_name="Number"),
    ]
    namespace_infos = [
        OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="UA"),
        OpcUaNamespaceInfo(uri="urn:unused", display_name="Unused"),
        OpcUaNamespaceInfo(uri="http://www.siemens.com/simatic-s7-opcua", display_name="Siemens"),
    ]

    assert objecttype_helpers._scalar_schema_from_type_hierarchy(hierarchy, namespace_infos) == expected
    assert objecttype_helpers._scalar_schema_from_type_hierarchy(hierarchy[:2] + hierarchy[3:], namespace_infos) == {
        "type": "integer"
    }


@pytest.mark.asyncio
async def test_custom_scalar_datatype_hierarchies_are_batched_and_keyed_by_expanded_id() -> None:
    source_type_id = "nsu=http://www.siemens.com/simatic-s7-opcua;i=3001"

    class Client:
        async def resolve_type_definition_hierarchies(self, type_ids: list[str]) -> dict[str, list[Any]]:
            assert type_ids == ["ns=1;i=3001"]
            return {
                "ns=1;i=3001": [
                    SimpleNamespace(node_id=source_type_id, browse_name="BYTE"),
                    SimpleNamespace(node_id="ns=0;i=28", browse_name="UInteger"),
                    SimpleNamespace(node_id="ns=0;i=3", browse_name="Byte"),
                ]
            }

    namespace_infos = [
        OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="UA"),
        OpcUaNamespaceInfo(uri="http://www.siemens.com/simatic-s7-opcua", display_name="Siemens"),
    ]
    result = await objecttype_helpers._resolve_custom_scalar_datatype_schemas(
        {source_type_id},
        namespace_infos,
        Client(),  # type: ignore[arg-type]
    )

    assert result == {source_type_id: {"type": "integer", "minimum": 0, "maximum": 255}}


@pytest.mark.asyncio
async def test_object_type_context_applies_resolved_custom_datatype_schema() -> None:
    source_type_id = "nsu=http://www.siemens.com/simatic-s7-opcua;i=3001"
    member = OpcUaObjectTypeInfo(
        node_id="ns=2;i=1000",
        parent_node_id=None,
        browse_name="DeviceType",
        display_name="DeviceType",
        properties={"Value": source_type_id},
        members=[
            OpcUaObjectTypeMemberInfo(
                node_id="ns=2;i=1001",
                browse_name="Value",
                display_name="Value",
                description=None,
                node_class="Variable",
                data_type=source_type_id,
                value_rank=-1,
            )
        ],
    )

    class Client:
        async def resolve_type_definition_hierarchies(self, type_ids: list[str]) -> dict[str, list[Any]]:
            assert type_ids == ["ns=2;i=3001"]
            return {
                "ns=2;i=3001": [
                    SimpleNamespace(node_id=source_type_id, browse_name="BYTE"),
                    SimpleNamespace(node_id="ns=0;i=28", browse_name="UInteger"),
                    SimpleNamespace(node_id="ns=0;i=3", browse_name="Byte"),
                ]
            }

    model = BuildResult(
        {
            "property": ModelNode(
                id="property",
                name="Value",
                kind="property",
                type=source_type_id,
                source_node_id="ns=2;i=1001",
            )
        },
        [],
        {},
        {},
        {},
        {},
    )
    context = await objecttype_helpers._build_object_type_context(
        model,
        [
            OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="UA"),
            OpcUaNamespaceInfo(uri="urn:unused", display_name="Unused"),
            OpcUaNamespaceInfo(uri="http://www.siemens.com/simatic-s7-opcua", display_name="Siemens"),
        ],
        Client(),  # type: ignore[arg-type]
        [member],
    )

    device_type = next(item for item in context.items if item.displayName == "DeviceType")
    assert device_type.schema_["properties"]["Value"]["minimum"] == 0
    assert device_type.schema_["properties"]["Value"]["maximum"] == 255
    datatype = next(item for item in context.items if item.sourceTypeId == source_type_id)
    scalar_branch = next(branch for branch in datatype.schema_["oneOf"] if branch.get("type") == "integer")
    assert scalar_branch == {"type": "integer", "minimum": 0, "maximum": 255}


def test_custom_scalar_datatype_object_type_uses_resolved_primitive_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(objecttype_helpers, "build_data_type_schema", lambda *_args: {"type": "object"})
    source_type_id = "nsu=http://www.siemens.com/simatic-s7-opcua;i=3001"
    response = objecttype_helpers._datatype_object_type_from_source_type_id(
        source_type_id,
        [
            OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="UA"),
            OpcUaNamespaceInfo(uri="http://www.siemens.com/simatic-s7-opcua", display_name="Siemens"),
        ],
        {"type": "integer", "minimum": 0, "maximum": 255},
    )

    assert response is not None
    assert response.displayName == "DataType_i_3001"
    scalar_branch = next(
        branch for branch in response.schema_["oneOf"] if branch.get("type") == "integer"
    )
    array_branch = next(branch for branch in response.schema_["oneOf"] if branch.get("type") == "array")
    assert scalar_branch == {"type": "integer", "minimum": 0, "maximum": 255}
    assert array_branch["items"] == scalar_branch


def test_standard_ua_datatype_scalar_schema_detection() -> None:
    schema = v1._scalar_schema_for_standard_ua_datatype_node_id("nsu=http://opcfoundation.org/UA/;i=95")
    assert schema == {"type": "integer"}

    structured = v1._scalar_schema_for_standard_ua_datatype_node_id("nsu=http://opcfoundation.org/UA/;i=865")
    assert structured == {"type": "object"}

    enum_like = v1._scalar_schema_for_standard_ua_datatype_node_id("nsu=http://opcfoundation.org/UA/;i=852")
    assert enum_like == {"type": "integer"}

    role_permission = v1._scalar_schema_for_standard_ua_datatype_node_id("nsu=http://opcfoundation.org/UA/;i=96")
    assert role_permission == {"type": "object"}

    type_like = v1._scalar_schema_for_standard_ua_datatype_node_id("nsu=http://opcfoundation.org/UA/;i=61")
    assert type_like is None

    non_datatype = v1._scalar_schema_for_standard_ua_datatype_node_id("nsu=http://opcfoundation.org/UA/;i=11492")
    assert non_datatype is None


def test_generic_object_type_from_source_type_id() -> None:
    infos = [
        OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="UA"),
        OpcUaNamespaceInfo(uri="http://example.com/custom", display_name="Custom"),
    ]

    class _NoopClient:
        async def read_browse_name(self, node_id: str) -> str | None:
            return None

    client = cast(Any, _NoopClient())

    standard = asyncio.run(
        v1._generic_object_type_from_source_type_id(
            "nsu=http://opcfoundation.org/UA/;i=865",
            infos,
            client,
            {},
            {"remaining": 0},
        )
    )
    assert standard is not None
    assert standard.displayName == "SessionDiagnosticsDataType"
    assert standard.schema_["type"] == "object"

    custom = asyncio.run(
        v1._generic_object_type_from_source_type_id(
            "nsu=http://example.com/custom;s=Vendor.TypeA",
            infos,
            client,
            {},
            {"remaining": 0},
        )
    )
    assert custom is not None
    assert custom.displayName == "TypeA"
    assert custom.namespaceUri == "http://example.com/custom"


def test_find_node_and_parent_helpers() -> None:
    model = _sample_model()
    assert v1._find_model_node(model, "prop-a") is not None
    assert v1._find_model_node(model, "Temperature") is not None
    assert v1._find_model_node(model, "ns=1;i=11") is not None
    assert v1._find_model_node(model, "missing") is None
    assert v1._parent_id_for_node(model, "prop-b") == "asset-child"
    assert v1._parent_id_for_node(model, "asset-root") is None


def test_collect_value_component_nodes_respects_depth() -> None:
    model = _sample_model()
    root = model.nodes_by_id["asset-root"]
    assert v1._collect_value_component_nodes(model, root, max_depth=1) == []
    depth_two = v1._collect_value_component_nodes(model, root, max_depth=2)
    assert [node.id for node in depth_two] == ["prop-a", "prop-b"]
    assert [node.id for node in v1._collect_value_component_nodes(model, root, max_depth=0)] == ["prop-a", "prop-b"]


def test_collect_history_source_nodes_paths() -> None:
    model = _sample_model()
    root = model.nodes_by_id["asset-root"]
    prop = model.nodes_by_id["prop-a"]
    assert [node.id for node in v1._collect_history_source_nodes(model, prop, 1)] == ["prop-a"]
    assert [node.id for node in v1._collect_history_source_nodes(model, root, 1)] == []
    assert {node.id for node in v1._collect_history_source_nodes(model, root, 0)} == {"prop-a", "prop-b"}


def test_parse_iso_and_history_range_validation() -> None:
    start = v1._parse_iso_datetime("2026-01-01T12:00:00", "startTime")
    assert start.tzinfo is not None

    body = v1.GetObjectHistoryRequest(
        elementIds=["x"],
        startTime="2026-01-01T00:00:00Z",
        endTime="2026-01-01T01:00:00Z",
        maxDepth=1,
    )
    parsed_start, parsed_end = v1._parse_history_time_range(body)
    assert parsed_start is not None
    assert parsed_end is not None
    assert parsed_start <= parsed_end

    with pytest.raises(HTTPException):
        v1._parse_iso_datetime("not-a-time", "startTime")

    bad = v1.GetObjectHistoryRequest(
        elementIds=["x"],
        startTime="2026-01-02T00:00:00Z",
        endTime="2026-01-01T00:00:00Z",
        maxDepth=1,
    )
    with pytest.raises(HTTPException):
        v1._parse_history_time_range(bad)


def test_quality_timestamp_and_history_conversion_helpers() -> None:
    assert v1._normalize_quality(None) == "Good"
    assert v1._normalize_quality(SimpleNamespace(is_good=lambda: False)) == "Bad"
    assert v1._normalize_quality(SimpleNamespace(name="UncertainLastUsableValue")) == "Uncertain"
    assert v1._normalize_quality(SimpleNamespace(name="GoodClamped")) == "Good"
    assert v1._normalize_quality(SimpleNamespace(name="SomethingElse")) == "Bad"

    naive_dt = datetime(2026, 1, 1, 12, 0, 0)
    aware_dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert v1._normalize_timestamp(naive_dt).endswith("Z")
    assert v1._normalize_timestamp(aware_dt).endswith("Z")
    assert v1._normalize_timestamp("invalid").endswith("Z")

    data_value = SimpleNamespace(
        Value=SimpleNamespace(Value=123),
        SourceTimestamp=aware_dt,
        StatusCode=SimpleNamespace(name="Good"),
    )
    vqt = v1._to_vqt_from_history_value(data_value)
    assert vqt.value == 123
    assert vqt.quality == "Good"
    assert vqt.timestamp.endswith("Z")


def test_not_implemented_and_server_info() -> None:
    with pytest.raises(HTTPException) as exc_info:
        v1._not_implemented("x")
    assert exc_info.value.status_code == 501

    caps = v1._supported_capabilities()
    assert caps.query.history is True
    info = v1._build_server_info()
    assert info.specVersion == "1.0"


def _context_model() -> BuildResult:
    return BuildResult(
        nodes_by_id={"n1": ModelNode(id="n1", name="n1", kind="asset", source_node_id="n1")},
        root_ids=["n1"],
        children_by_id={},
        instances_by_type_id={},
        property_to_node={},
        action_to_method={},
    )


class _EmptyOpcUaClient:
    async def get_namespace_infos(self) -> list[object]:
        return []

    async def get_object_types(self) -> list[object]:
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [objecttype_helpers, v1])
async def test_type_context_shares_schemas_without_changing_responses(
    monkeypatch: pytest.MonkeyPatch, module: Any
) -> None:
    types = [
        OpcUaObjectTypeInfo(f"i={index}", None, f"Type{index}", f"Type{index}", {"Name": "i=12"})
        for index in (10001, 10002)
    ]
    model = _context_model()
    client = cast(OpcUaClientProtocol, _EmptyOpcUaClient())
    with monkeypatch.context() as patch:
        patch.setattr(module, "deduplicate_schema_fragments", list)
        original = await module._build_object_type_context(model, [], client, types)
    compact = await module._build_object_type_context(model, [], client, types)
    assert [item.model_dump_json() for item in compact.items] == [item.model_dump_json() for item in original.items]
    seen: dict[str, object] = {}
    duplicates = 0

    def check(value: Any) -> None:
        nonlocal duplicates
        if isinstance(value, (dict, list)):
            key = json.dumps(value, separators=(",", ":"), ensure_ascii=True)
            if key in seen:
                assert seen[key] is value
                duplicates += 1
            seen[key] = value
            for child in value.values() if isinstance(value, dict) else value:
                check(child)

    for item in compact.items:
        check(item.schema_)
    assert duplicates > 0


@pytest.mark.asyncio
async def test_object_type_context_cache_hit_skips_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _context_model()
    builds = 0

    async def _build(**_kwargs: Any) -> objecttype_helpers._ObjectTypeContext:
        nonlocal builds
        builds += 1
        return objecttype_helpers._ObjectTypeContext([], [], {}, [], {})

    monkeypatch.setattr(objecttype_helpers, "_build_object_type_context", _build)
    lock = asyncio.Lock()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(object_type_lock=lock)))
    client = cast(Any, _EmptyOpcUaClient())

    first = await objecttype_helpers._get_object_type_context(request, model, client)

    await lock.acquire()
    try:
        second = await asyncio.wait_for(objecttype_helpers._get_object_type_context(request, model, client), 1)
    finally:
        lock.release()

    assert second is first
    assert builds == 1


@pytest.mark.asyncio
async def test_object_type_context_cache_hits_for_equivalent_rebuilt_model(monkeypatch: pytest.MonkeyPatch) -> None:
    builds = 0

    async def _build(**_kwargs: Any) -> objecttype_helpers._ObjectTypeContext:
        nonlocal builds
        builds += 1
        return objecttype_helpers._ObjectTypeContext([], [], {}, [], {})

    monkeypatch.setattr(objecttype_helpers, "_build_object_type_context", _build)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(object_type_lock=asyncio.Lock())))
    client = cast(Any, _EmptyOpcUaClient())
    model_a = _context_model()
    model_b = _context_model()

    first = await objecttype_helpers._get_object_type_context(request, model_a, client)
    second = await objecttype_helpers._get_object_type_context(request, model_b, client)

    assert second is first
    assert builds == 1
    assert getattr(request.app.state, "object_type_refresh_task", None) is None

    changed = _context_model()
    changed.nodes_by_id["n2"] = ModelNode(id="n2", name="n2", kind="asset", source_node_id="n2")
    stale = await objecttype_helpers._get_object_type_context(request, changed, client)
    assert stale is first
    await request.app.state.object_type_refresh_task
    third = await objecttype_helpers._get_object_type_context(request, changed, client)

    assert third is not first
    assert builds == 2


@pytest.mark.asyncio
async def test_object_type_context_serves_stale_while_rebuild_holds_lock(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    builds = 0
    release = asyncio.Event()

    async def _build(**_kwargs: Any) -> objecttype_helpers._ObjectTypeContext:
        nonlocal builds
        builds += 1
        if builds > 1:
            await release.wait()
        return objecttype_helpers._ObjectTypeContext([], [], {}, [], {})

    monkeypatch.setattr(objecttype_helpers, "_build_object_type_context", _build)
    state = SimpleNamespace(object_type_lock=asyncio.Lock())
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    client = cast(Any, _EmptyOpcUaClient())
    first = await objecttype_helpers._get_object_type_context(request, _context_model(), client)

    newest = _context_model()
    newest.nodes_by_id["n2"] = ModelNode(id="n2", name="n2", kind="asset", source_node_id="n2")
    state.model_cache = newest
    request_model = _context_model()
    request_model.nodes_by_id["old"] = ModelNode(id="old", name="old", kind="asset", source_node_id="old")
    with caplog.at_level("WARNING"):
        results = await asyncio.wait_for(
            asyncio.gather(
                *(objecttype_helpers._get_object_type_context(request, request_model, client) for _ in range(10))
            ),
            1,
        )

    stale_logs = [r for r in caplog.records if "stale object type context" in r.getMessage()]
    assert len(stale_logs) == 10
    assert all(r.levelname == "WARNING" for r in stale_logs)
    assert all(result is first for result in results)
    refresh_task = state.object_type_refresh_task
    await asyncio.sleep(0)
    assert state.object_type_lock.locked()
    release.set()
    await refresh_task
    assert builds == 2
    assert state.object_type_context_cache["model_token"] == model_revision(newest)
    assert state.object_type_context_cache["context"] is not first


@pytest.mark.asyncio
async def test_object_type_context_cold_cache_and_warm_up_wait_for_build(monkeypatch: pytest.MonkeyPatch) -> None:
    builds = 0

    async def _build(**_kwargs: Any) -> objecttype_helpers._ObjectTypeContext:
        nonlocal builds
        builds += 1
        return objecttype_helpers._ObjectTypeContext([], [], {}, [], {})

    monkeypatch.setattr(objecttype_helpers, "_build_object_type_context", _build)
    app = SimpleNamespace(state=SimpleNamespace(object_type_lock=asyncio.Lock()))
    client = cast(Any, _EmptyOpcUaClient())
    model = _context_model()
    first = await objecttype_helpers._get_object_type_context(SimpleNamespace(app=app), model, client)

    changed = _context_model()
    changed.nodes_by_id["n2"] = ModelNode(id="n2", name="n2", kind="asset", source_node_id="n2")
    warmed = await objecttype_helpers.warm_object_type_context(app, changed, client)

    assert builds == 2
    assert warmed is not first
    assert app.state.object_type_context_cache["model_token"] == model_revision(changed)
    assert getattr(app.state, "object_type_refresh_task", None) is None


@pytest.mark.asyncio
async def test_failed_background_object_type_refresh_keeps_stale_context(monkeypatch: pytest.MonkeyPatch) -> None:
    builds = 0

    async def _build(**_kwargs: Any) -> objecttype_helpers._ObjectTypeContext:
        nonlocal builds
        builds += 1
        if builds > 1:
            raise RuntimeError("rebuild failed")
        return objecttype_helpers._ObjectTypeContext([], [], {}, [], {})

    monkeypatch.setattr(objecttype_helpers, "_build_object_type_context", _build)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(object_type_lock=asyncio.Lock())))
    client = cast(Any, _EmptyOpcUaClient())
    first = await objecttype_helpers._get_object_type_context(request, _context_model(), client)
    changed = _context_model()
    changed.nodes_by_id["n2"] = ModelNode(id="n2", name="n2", kind="asset", source_node_id="n2")

    assert await objecttype_helpers._get_object_type_context(request, changed, client) is first
    await request.app.state.object_type_refresh_task
    assert request.app.state.object_type_context_cache["context"] is first
    assert await objecttype_helpers._get_object_type_context(request, changed, client) is first


@pytest.mark.asyncio
async def test_object_type_definition_change_with_same_node_id_rebuilds_and_changes_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builds = 0

    async def _build(**kwargs: Any) -> objecttype_helpers._ObjectTypeContext:
        nonlocal builds
        builds += 1
        served = {item.node_id: item.display_name for item in kwargs["object_types"]}
        return objecttype_helpers._ObjectTypeContext([], [], served, [], {})

    monkeypatch.setattr(objecttype_helpers, "_build_object_type_context", _build)

    class _TypesClient(_EmptyOpcUaClient):
        def __init__(self) -> None:
            self.types = [SimpleNamespace(node_id="ns=1;i=1", display_name="Pump")]

        async def get_object_types(self) -> list[object]:
            return list(self.types)

    client = _TypesClient()
    app = SimpleNamespace(state=SimpleNamespace(object_type_lock=asyncio.Lock()))
    model = _context_model()
    first = await objecttype_helpers.warm_object_type_context(app, model, cast(Any, client))

    client.types = [SimpleNamespace(node_id="ns=1;i=1", display_name="Pump v2")]
    changed = await objecttype_helpers.warm_object_type_context(app, model, cast(Any, client))

    assert builds == 2
    assert changed.revision != first.revision

    # A fresh process rebuilding from the same inputs serves the same revision.
    other_app = SimpleNamespace(state=SimpleNamespace(object_type_lock=asyncio.Lock()))
    rebuilt = await objecttype_helpers.warm_object_type_context(other_app, model, cast(Any, client))
    assert rebuilt.revision == changed.revision


def test_object_types_token_is_memoized_per_object_types_list(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    original = objecttype_helpers._object_types_token

    def _counting(object_types: list[Any]) -> str:
        nonlocal calls
        calls += 1
        return original(object_types)

    monkeypatch.setattr(objecttype_helpers, "_object_types_token", _counting)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    cached_list: list[Any] = [SimpleNamespace(node_id="ns=1;i=1", display_name="Pump")]

    first = objecttype_helpers._memoized_object_types_token(request, cached_list)
    assert objecttype_helpers._memoized_object_types_token(request, cached_list) == first
    assert calls == 1

    # An equal but distinct list (a metadata reload) is hashed again and yields the same token.
    reloaded: list[Any] = [SimpleNamespace(node_id="ns=1;i=1", display_name="Pump")]
    assert objecttype_helpers._memoized_object_types_token(request, reloaded) == first
    changed: list[Any] = [SimpleNamespace(node_id="ns=1;i=1", display_name="Pump v2")]
    assert objecttype_helpers._memoized_object_types_token(request, changed) != first
    assert calls == 3


def test_object_types_token_is_stable_for_unrepresentable_member_values() -> None:
    def _types(value: object, display_name: str = "Pump") -> list[Any]:
        member = SimpleNamespace(node_id="ns=1;i=2", browse_name="m", display_name="M", value=value)
        return [SimpleNamespace(node_id="ns=1;i=1", display_name=display_name, properties={}, members=[member])]

    assert objecttype_helpers._object_types_token(_types(object())) == objecttype_helpers._object_types_token(
        _types(object())
    )
    assert objecttype_helpers._object_types_token(_types(None)) != objecttype_helpers._object_types_token(
        _types(None, display_name="Pump v2")
    )


def test_parent_id_for_node_uses_reverse_index_with_original_precedence() -> None:
    from i3x_server.api.v1 import object_helpers

    def _model(children_by_id: dict[str, list[str]]) -> BuildResult:
        return BuildResult(
            nodes_by_id={},
            root_ids=["root"],
            children_by_id=children_by_id,
            instances_by_type_id={},
            property_to_node={},
            action_to_method={},
            hierarchy_children_by_id={"h-parent": ["shared"], "h-other": ["shared", "only-h"]},
        )

    model = _model({"c-parent": ["shared", "only-c"], "c-other": ["only-c"]})
    assert object_helpers._parent_id_for_node(model, "shared") == "h-parent"
    assert object_helpers._parent_id_for_node(model, "only-h") == "h-other"
    assert object_helpers._parent_id_for_node(model, "only-c") == "c-parent"
    assert object_helpers._parent_id_for_node(model, "root") is None
    assert object_helpers._parent_id_for_node(model, "missing") is None

    replaced = _model({"c-new": ["only-c"]})
    assert object_helpers._parent_id_for_node(replaced, "only-c") == "c-new"


def test_reverse_parent_cache_does_not_retain_discarded_model() -> None:
    model = _sample_model()
    node_ref = weakref.ref(model.nodes_by_id["asset-root"])
    assert object_helpers._reverse_parent_index(model)

    del model
    gc.collect()

    assert node_ref() is None


def test_reverse_parent_cache_is_reused_per_model_and_invalidated_on_size_change() -> None:
    model = _sample_model()
    original_index = object_helpers._reverse_parent_index(model)
    assert object_helpers._reverse_parent_index(model) is original_index

    other_model = _sample_model()
    assert object_helpers._reverse_parent_index(other_model) is not original_index
    assert object_helpers._reverse_parent_index(model) is original_index

    model.children_by_id["new-parent"] = ["new-child"]
    updated_index = object_helpers._reverse_parent_index(model)
    assert updated_index is not original_index
    assert updated_index["new-child"] == "new-parent"
