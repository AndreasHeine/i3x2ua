from i3x_server.model.mapper import classify_opcua_reference, map_node
from i3x_server.opcua.client import OpcUaNodeInfo


def test_map_variable_to_property() -> None:
    node = OpcUaNodeInfo(
        node_id="ns=2;s=Temperature",
        parent_node_id="ns=2;s=Machine",
        browse_name="Temperature",
        display_name="Temperature",
        node_class="Variable",
        data_type="Double",
        event_notifier=False,
    )

    mapped = map_node(node, [])

    assert mapped.kind == "property"
    assert mapped.type == "Double"
    assert mapped.source_node_id == "ns=2;s=Temperature"


def test_classify_opcua_reference_hierarchy_organizes() -> None:
    result = classify_opcua_reference("ns=0;i=35", "Organizes")
    assert result == "hierarchy"


def test_classify_opcua_reference_composition_hascomponent() -> None:
    result = classify_opcua_reference("ns=0;i=47", "HasComponent", target_node_class="Variable")
    assert result == "composition"


def test_classify_opcua_reference_hierarchy_hascomponent_to_object() -> None:
    result = classify_opcua_reference("ns=0;i=47", "HasComponent", target_node_class="Object")
    assert result == "hierarchy"


def test_classify_opcua_reference_composition_hasproperty() -> None:
    result = classify_opcua_reference("ns=0;i=46", "HasProperty")
    assert result == "composition"


def test_classify_opcua_reference_type_meta_hastypedefinition() -> None:
    result = classify_opcua_reference("ns=0;i=40", "HasTypeDefinition")
    assert result == "type-meta"


def test_classify_opcua_reference_type_meta_hassubtype() -> None:
    result = classify_opcua_reference("ns=0;i=45", "HasSubtype")
    assert result == "type-meta"


def test_classify_opcua_reference_graph_nonhierarchical() -> None:
    result = classify_opcua_reference("ns=0;i=32", "NonHierarchicalReferences")
    assert result == "graph"


def test_classify_opcua_reference_subtype_of_organizes_maps_hierarchy() -> None:
    result = classify_opcua_reference(
        "ns=2;i=1234",
        "VendorOrganizesSubtype",
        supertype_browse_names=["HierarchicalReferences", "Organizes"],
    )
    assert result == "hierarchy"


def test_classify_opcua_reference_subtype_of_hascomponent_maps_composition() -> None:
    result = classify_opcua_reference(
        "ns=2;i=9999",
        "VendorComposedBy",
        supertype_browse_names=["Aggregates", "HasComponent"],
    )
    assert result == "composition"


def test_classify_opcua_reference_nonhierarchical_root_wins_over_hierarchical() -> None:
    result = classify_opcua_reference(
        "ns=2;i=7777",
        "VendorAmbiguous",
        supertype_browse_names=["HasComponent", "HierarchicalReferences", "NonHierarchicalReferences"],
        target_node_class="Object",
    )
    assert result == "graph"


def test_classify_opcua_reference_hierarchical_root_defaults_to_hierarchy() -> None:
    result = classify_opcua_reference(
        "ns=2;i=8888",
        "VendorHierarchyOnly",
        supertype_browse_names=["HierarchicalReferences", "HasChild"],
    )
    assert result == "hierarchy"


def test_classify_opcua_reference_unknown_defaults_to_graph() -> None:
    result = classify_opcua_reference("ns=2;i=2222", "UnknownVendorReference")
    assert result == "graph"
