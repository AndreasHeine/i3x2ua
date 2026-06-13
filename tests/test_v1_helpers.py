from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException

from i3x_server.api import v1
from i3x_server.opcua.client import OpcUaNamespaceInfo
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult


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
