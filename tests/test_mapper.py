from i3x_server.infrastructure.opcua.client import OpcUaNodeInfo
from i3x_server.model.mapper import classify_opcua_reference, classify_opcua_reference_with_confidence, map_node
from i3x_server.model.semantic_profiles import SemanticProfile, resolve_namespace_uri


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


def test_profile_classification_prefers_hascomponent_for_variable_targets() -> None:
    generic_profile = SemanticProfile(
        profile_id="generic",
        priority=10,
        namespace_uri_fragment="",
        hierarchy_references=("HierarchicalReferences",),
        composition_references=("HasComponent",),
        graph_references=("NonHierarchicalReferences",),
    )

    classification, confidence = classify_opcua_reference_with_confidence(
        reference_type_node_id="ns=2;i=5001",
        reference_browse_name="VendorComposedBy",
        supertype_browse_names=["HasComponent", "HierarchicalReferences"],
        target_node_class="Variable",
        profiles=(generic_profile,),
    )

    assert classification == "composition"
    assert confidence == "medium"


def test_profile_classification_keeps_hascomponent_hierarchy_for_object_targets() -> None:
    generic_profile = SemanticProfile(
        profile_id="generic",
        priority=10,
        namespace_uri_fragment="",
        hierarchy_references=("HierarchicalReferences",),
        composition_references=("HasComponent",),
        graph_references=("NonHierarchicalReferences",),
    )

    classification, confidence = classify_opcua_reference_with_confidence(
        reference_type_node_id="ns=2;i=5001",
        reference_browse_name="VendorComposedBy",
        supertype_browse_names=["HasComponent", "HierarchicalReferences"],
        target_node_class="Object",
        profiles=(generic_profile,),
    )

    assert classification == "hierarchy"
    assert confidence == "high"


def test_resolve_namespace_uri_defaults_bare_node_id_to_ns_zero() -> None:
    namespace_map = {0: "http://opcfoundation.org/UA/", 2: "urn:vendor:test"}

    assert resolve_namespace_uri("i=14117", namespace_map) == "http://opcfoundation.org/UA/"


def test_resolve_namespace_uri_prefers_explicit_namespace_index() -> None:
    namespace_map = {0: "http://opcfoundation.org/UA/", 2: "urn:vendor:test"}

    assert resolve_namespace_uri("ns=2;s=Temperature", namespace_map) == "urn:vendor:test"
