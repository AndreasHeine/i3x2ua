from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from i3x_server.api import beta
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
    )


def test_display_name_for_uri_variants() -> None:
    assert beta._display_name_for_uri("https://cesmii.org/i3x") == "I3X"
    assert beta._display_name_for_uri("https://example.com/ns/device_model") == "Device Model"
    assert beta._display_name_for_uri("https://example.com/ns/a12") == "A12"


def test_namespace_and_expanded_node_helpers() -> None:
    infos = [
        OpcUaNamespaceInfo(uri="http://default", display_name="Default"),
        OpcUaNamespaceInfo(uri="http://custom", display_name="Custom"),
    ]
    assert beta._namespace_uri_for_node_id("ns=1;i=42", infos) == "http://custom"
    assert beta._namespace_uri_for_node_id("ns=99;i=42", infos) == ""
    assert beta._expanded_node_id("ns=1;i=42", infos) == "nsu=http://custom;i=42"
    assert beta._expanded_node_id("nsu=http://custom;i=42", infos) == "nsu=http://custom;i=42"
    assert beta._expanded_node_id("asset-root", infos) == "asset-root"
    assert beta._namespace_uri_from_expanded_node_id("nsu=http://custom;i=42") == "http://custom"
    assert beta._namespace_uri_from_expanded_node_id("ns=1;i=42") is None


def test_element_and_urn_token_helpers() -> None:
    assert beta._to_element_id("MachineType") == "machine-type"
    assert beta._to_element_id("__") == "unknown-type"
    assert beta._to_urn_token("HTTP://Example.COM/Plant 01") == "http-example-com-plant-01"


def test_find_node_and_parent_helpers() -> None:
    model = _sample_model()
    assert beta._find_model_node(model, "prop-a") is not None
    assert beta._find_model_node(model, "Temperature") is not None
    assert beta._find_model_node(model, "ns=1;i=11") is not None
    assert beta._find_model_node(model, "missing") is None
    assert beta._parent_id_for_node(model, "prop-b") == "asset-child"
    assert beta._parent_id_for_node(model, "asset-root") is None


def test_collect_value_component_nodes_respects_depth() -> None:
    model = _sample_model()
    root = model.nodes_by_id["asset-root"]
    assert beta._collect_value_component_nodes(model, root, max_depth=1) == []
    depth_two = beta._collect_value_component_nodes(model, root, max_depth=2)
    assert [node.id for node in depth_two] == ["prop-a", "prop-b"]
    assert [node.id for node in beta._collect_value_component_nodes(model, root, max_depth=0)] == ["prop-a", "prop-b"]


def test_collect_history_source_nodes_paths() -> None:
    model = _sample_model()
    root = model.nodes_by_id["asset-root"]
    prop = model.nodes_by_id["prop-a"]
    assert [node.id for node in beta._collect_history_source_nodes(model, prop, 1)] == ["prop-a"]
    assert [node.id for node in beta._collect_history_source_nodes(model, root, 1)] == []
    assert {node.id for node in beta._collect_history_source_nodes(model, root, 0)} == {"prop-a", "prop-b"}


def test_parse_iso_and_history_range_validation() -> None:
    start = beta._parse_iso_datetime("2026-01-01T12:00:00", "startTime")
    assert start.tzinfo is not None

    body = beta.GetObjectHistoryRequest(
        elementIds=["x"],
        startTime="2026-01-01T00:00:00Z",
        endTime="2026-01-01T01:00:00Z",
        maxDepth=1,
    )
    parsed_start, parsed_end = beta._parse_history_time_range(body)
    assert parsed_start is not None
    assert parsed_end is not None
    assert parsed_start <= parsed_end

    with pytest.raises(HTTPException):
        beta._parse_iso_datetime("not-a-time", "startTime")

    bad = beta.GetObjectHistoryRequest(
        elementIds=["x"],
        startTime="2026-01-02T00:00:00Z",
        endTime="2026-01-01T00:00:00Z",
        maxDepth=1,
    )
    with pytest.raises(HTTPException):
        beta._parse_history_time_range(bad)


def test_quality_timestamp_and_history_conversion_helpers() -> None:
    assert beta._normalize_quality(None) == "Good"
    assert beta._normalize_quality(SimpleNamespace(is_good=lambda: False)) == "Bad"
    assert beta._normalize_quality(SimpleNamespace(name="UncertainLastUsableValue")) == "Uncertain"
    assert beta._normalize_quality(SimpleNamespace(name="GoodClamped")) == "Good"
    assert beta._normalize_quality(SimpleNamespace(name="SomethingElse")) == "Bad"

    naive_dt = datetime(2026, 1, 1, 12, 0, 0)
    aware_dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert beta._normalize_timestamp(naive_dt).endswith("Z")
    assert beta._normalize_timestamp(aware_dt).endswith("Z")
    assert beta._normalize_timestamp("invalid").endswith("Z")

    data_value = SimpleNamespace(
        Value=SimpleNamespace(Value=123),
        SourceTimestamp=aware_dt,
        StatusCode=SimpleNamespace(name="Good"),
    )
    vqt = beta._to_vqt_from_history_value(data_value)
    assert vqt.value == 123
    assert vqt.quality == "Good"
    assert vqt.timestamp.endswith("Z")


def test_not_implemented_and_server_info() -> None:
    with pytest.raises(HTTPException) as exc_info:
        beta._not_implemented("x")
    assert exc_info.value.status_code == 501

    caps = beta._supported_capabilities()
    assert caps.query.history is True
    info = beta._build_server_info()
    assert info.specVersion == "1.0"
