from __future__ import annotations

from i3x_server.domain.ports.opcua import OpcUaNodeInfo
from i3x_server.model import (
    _classify_hierarchical_family,
    _normalize_token,
    classify_opcua_reference,
    infer_kind,
    map_node,
    map_type,
    stable_i3x_id,
)


def _node(
    node_class: str = "Object",
    event_notifier: bool = False,
    display_name: str = "Display",
    browse_name: str = "Browse",
    data_type: str | None = "Double",
) -> OpcUaNodeInfo:
    return OpcUaNodeInfo(
        node_id="ns=2;s=Node",
        parent_node_id=None,
        browse_name=browse_name,
        display_name=display_name,
        node_class=node_class,
        data_type=data_type,
        type_definition_id="ns=0;i=58",
        event_notifier=event_notifier,
    )


def test_normalize_token_covers_all_normalization_rules() -> None:
    assert _normalize_token(None) == ""
    assert _normalize_token("") == ""
    assert _normalize_token("  ") == ""
    assert _normalize_token("ns=0;i=35") == "i35"
    assert _normalize_token("0:HasComponent") == "hascomponent"
    assert _normalize_token("http://example.com/ref/HasProperty") == "hasproperty"


def test_classify_hierarchical_family_branches() -> None:
    assert _classify_hierarchical_family({"hasproperty"}, "Variable") == "composition"
    assert _classify_hierarchical_family({"hascomponent"}, "Object") == "hierarchy"
    assert _classify_hierarchical_family({"hascomponent"}, "Variable") == "composition"
    assert _classify_hierarchical_family({"hascomponent"}, None) == "composition"
    assert _classify_hierarchical_family({"organizes"}, "Object") == "hierarchy"


def test_classify_opcua_reference_type_meta_and_roots() -> None:
    assert classify_opcua_reference("ns=0;i=40", "HasTypeDefinition") == "type-meta"
    assert classify_opcua_reference("ns=0;i=45", "HasSubtype") == "type-meta"
    assert classify_opcua_reference("ns=0;i=32", "NonHierarchicalReferences") == "graph"
    assert classify_opcua_reference("ns=0;i=33", "HierarchicalReferences") == "hierarchy"


def test_classify_opcua_reference_root_precedence_and_fallbacks() -> None:
    assert (
        classify_opcua_reference(
            "ns=1;i=9999",
            "VendorRef",
            supertype_browse_names=["HierarchicalReferences", "NonHierarchicalReferences"],
        )
        == "graph"
    )
    assert classify_opcua_reference("ns=0;i=46", "HasProperty", target_node_class="Variable") == "composition"
    assert classify_opcua_reference("ns=0;i=47", "HasComponent", target_node_class="Object") == "hierarchy"
    assert classify_opcua_reference("ns=0;i=47", "HasComponent", target_node_class="Variable") == "composition"
    assert classify_opcua_reference("ns=0;i=35", "Organizes") == "hierarchy"
    assert classify_opcua_reference("ns=2;i=2222", "UnknownVendorReference") == "graph"


def test_stable_i3x_id_is_deterministic_and_prefixed() -> None:
    one = stable_i3x_id("ns=2;s=Node", "asset")
    two = stable_i3x_id("ns=2;s=Node", "asset")
    assert one == two
    assert one.startswith("asset-")
    assert len(one.split("-", 1)[1]) == 16


def test_infer_kind_prefers_event_source_then_class_mapping() -> None:
    assert infer_kind(_node(event_notifier=True)) == "eventSource"
    assert infer_kind(_node(node_class="Variable")) == "property"
    assert infer_kind(_node(node_class="Method")) == "action"
    assert infer_kind(_node(node_class="UnknownClass")) == "asset"


def test_map_type_only_for_properties() -> None:
    property_node = _node(node_class="Variable", data_type="Boolean")
    asset_node = _node(node_class="Object", data_type="Boolean")
    assert map_type(property_node, "property") == "Boolean"
    assert map_type(asset_node, "asset") is None


def test_map_node_maps_fields_and_display_name_fallback() -> None:
    node_with_display = _node(display_name="Pump 1", browse_name="Pump_1")
    mapped = map_node(node_with_display, ["child-1"])
    assert mapped.name == "Pump 1"
    assert mapped.children == ["child-1"]
    assert mapped.source_node_id == "ns=2;s=Node"
    assert mapped.kind == "asset"

    node_without_display = _node(display_name="", browse_name="FallbackBrowse")
    mapped_fallback = map_node(node_without_display, [])
    assert mapped_fallback.name == "FallbackBrowse"
