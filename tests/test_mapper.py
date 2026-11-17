from i3x_server.domain.ports.opcua import OpcUaTypeDefinitionInfo
from i3x_server.infrastructure.opcua.client import OpcUaNodeInfo
from i3x_server.model.mapper import classify_opcua_reference, classify_opcua_reference_with_confidence, map_node
from i3x_server.model.semantic_profiles import (
    ProfileOverride,
    ResolvedProfileSet,
    SemanticProfile,
    active_profiles_for_node,
    forced_relationship_class_for_node,
    has_profile_override_for_node,
    load_default_profiles,
    resolve_namespace_uri,
    resolve_semantic_role,
)


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
        namespace_uri="",
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
        namespace_uri="",
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


def test_machinery_profile_matches_type_ancestor_namespace_not_instance_namespace() -> None:
    """The Machinery profile activates from the canonical Machinery namespace URI.

    Realistic OPC 40001-1 hierarchy: a vendor MachineToolType derives from the
    Machinery ``MachineryItemIdentificationType``, which itself derives from the DI
    ``FunctionalGroupType`` (OPC 10000-100 §4.4.1). The namespace constraint is
    satisfied on the Machinery entry and the override identity on that same entry,
    while the DI ancestor sits in a different namespace.
    """
    node = OpcUaNodeInfo(
        node_id="ns=1;s=Machine",
        parent_node_id=None,
        browse_name="Machine",
        display_name="Machine",
        node_class="Object",
        data_type=None,
        type_definition_id="ns=2;i=26",
    )
    type_hierarchy = (
        OpcUaTypeDefinitionInfo(
            node_id="ns=2;i=26",
            browse_name="MachineToolType",
            namespace_uri="http://opcfoundation.org/UA/MachineTool/",
        ),
        OpcUaTypeDefinitionInfo(
            node_id="nsu=http://opcfoundation.org/UA/Machinery/;i=1004",
            browse_name="MachineryItemIdentificationType",
            namespace_uri="http://opcfoundation.org/UA/Machinery/",
        ),
        OpcUaTypeDefinitionInfo(
            node_id="nsu=http://opcfoundation.org/UA/DI/;i=1005",
            browse_name="FunctionalGroupType",
            namespace_uri="http://opcfoundation.org/UA/DI/",
        ),
    )

    active_profiles = active_profiles_for_node(node, type_hierarchy, load_default_profiles())

    assert active_profiles.profile_ids == ("machinery", "generic")
    assert (
        resolve_semantic_role(
            node,
            active_profiles,
            type_definition_hierarchy=type_hierarchy,
        )
        == "asset"
    )
    assert has_profile_override_for_node(node, active_profiles, type_definition_hierarchy=type_hierarchy)
    assert forced_relationship_class_for_node(node, active_profiles, type_hierarchy) == "hierarchy"


def test_machinery_profile_activates_from_namespace_entry_alone() -> None:
    """A Machinery-namespace type in the chain identifies the profile on its own.

    Membership in the canonical Machinery namespace (OPC 40001-1 §20.1) is sufficient;
    the same entry must not additionally be named ``FunctionalGroupType`` or
    ``DataItemType``, and no known-type coupling is required.
    """
    node = OpcUaNodeInfo(
        node_id="ns=1;s=Equipment",
        parent_node_id=None,
        browse_name="Equipment",
        display_name="Equipment",
        node_class="Object",
        data_type=None,
        type_definition_id="ns=2;i=40",
    )
    type_hierarchy = (
        OpcUaTypeDefinitionInfo(
            node_id="ns=2;i=40",
            browse_name="VendorEquipmentType",
            namespace_uri="urn:vendor:types",
        ),
        OpcUaTypeDefinitionInfo(
            node_id="nsu=http://opcfoundation.org/UA/Machinery/;i=1013",
            browse_name="MachineryEquipmentFolderType",
            namespace_uri="http://opcfoundation.org/UA/Machinery/",
        ),
    )

    active_profiles = active_profiles_for_node(node, type_hierarchy, load_default_profiles())

    assert active_profiles.profile_ids == ("machinery", "generic")


def test_di_functional_group_type_alone_does_not_activate_machinery_profile() -> None:
    """The DI ``FunctionalGroupType`` (OPC 10000-100 §4.4.1) is not Machinery evidence."""
    node = OpcUaNodeInfo(
        node_id="ns=1;s=Group",
        parent_node_id=None,
        browse_name="Group",
        display_name="Group",
        node_class="Object",
        data_type=None,
        type_definition_id="nsu=http://opcfoundation.org/UA/DI/;i=1005",
    )
    type_hierarchy = (
        OpcUaTypeDefinitionInfo(
            node_id="nsu=http://opcfoundation.org/UA/DI/;i=1005",
            browse_name="FunctionalGroupType",
            namespace_uri="http://opcfoundation.org/UA/DI/",
        ),
    )

    active_profiles = active_profiles_for_node(node, type_hierarchy, load_default_profiles())

    assert active_profiles.profile_ids == ("generic",)
    assert resolve_semantic_role(node, active_profiles, type_definition_hierarchy=type_hierarchy) == "asset"
    assert not has_profile_override_for_node(node, active_profiles, type_definition_hierarchy=type_hierarchy)


def test_machinery_profile_does_not_match_unrelated_namespace_uri_containing_machinery() -> None:
    """Namespace comparison is an exact URI compare, not a loose substring match.

    ``http://opcfoundation.org/UA/Machinery/ProcessValues/`` is a distinct namespace
    and must not activate the Machinery profile.
    """
    node = OpcUaNodeInfo(
        node_id="ns=1;s=ProcessValue",
        parent_node_id=None,
        browse_name="ProcessValue",
        display_name="ProcessValue",
        node_class="Object",
        data_type=None,
        type_definition_id="ns=2;i=50",
    )
    type_hierarchy = (
        OpcUaTypeDefinitionInfo(
            node_id="ns=2;i=50",
            browse_name="VendorProcessValueType",
            namespace_uri="http://opcfoundation.org/UA/Machinery/ProcessValues/",
        ),
    )

    active_profiles = active_profiles_for_node(node, type_hierarchy, load_default_profiles())

    assert active_profiles.profile_ids == ("generic",)


def test_machinery_profile_matches_namespace_uri_without_trailing_slash() -> None:
    """Canonical namespace comparison tolerates the trailing-slash spelling only."""
    node = OpcUaNodeInfo(
        node_id="ns=1;s=Equipment",
        parent_node_id=None,
        browse_name="Equipment",
        display_name="Equipment",
        node_class="Object",
        data_type=None,
        type_definition_id="ns=2;i=40",
    )
    type_hierarchy = (
        OpcUaTypeDefinitionInfo(
            node_id="ns=2;i=40",
            browse_name="MachineryEquipmentFolderType",
            namespace_uri="http://opcfoundation.org/UA/Machinery",
        ),
    )

    active_profiles = active_profiles_for_node(node, type_hierarchy, load_default_profiles())

    assert active_profiles.profile_ids == ("machinery", "generic")


def test_resolve_semantic_role_object_container_is_asset_not_property() -> None:
    """Objects with composition children stay assets; "property" is reserved for overrides."""
    container = OpcUaNodeInfo(
        node_id="ns=1;s=Container",
        parent_node_id=None,
        browse_name="Container",
        display_name="Container",
        node_class="Object",
        data_type=None,
        type_definition_id=None,
    )
    variable = OpcUaNodeInfo(
        node_id="ns=1;s=Temperature",
        parent_node_id="ns=1;s=Container",
        browse_name="Temperature",
        display_name="Temperature",
        node_class="Variable",
        data_type="Double",
        type_definition_id=None,
    )
    method = OpcUaNodeInfo(
        node_id="ns=1;s=Reset",
        parent_node_id="ns=1;s=Container",
        browse_name="Reset",
        display_name="Reset",
        node_class="Method",
        data_type=None,
        type_definition_id=None,
    )

    active_profiles = active_profiles_for_node(container, (), load_default_profiles())

    assert resolve_semantic_role(container, active_profiles) == "asset"
    assert resolve_semantic_role(variable, active_profiles) == "datapoint"
    assert resolve_semantic_role(method, active_profiles) == "component"


def test_machinery_profile_does_not_match_same_type_name_in_other_namespace() -> None:
    node = OpcUaNodeInfo(
        node_id="ns=1;s=Machine",
        parent_node_id=None,
        browse_name="Machine",
        display_name="Machine",
        node_class="Object",
        data_type=None,
        type_definition_id="ns=2;i=26",
    )
    type_hierarchy = (
        OpcUaTypeDefinitionInfo(
            node_id="ns=2;i=26",
            browse_name="FunctionalGroupType",
            namespace_uri="urn:unrelated:types",
        ),
    )

    active_profiles = active_profiles_for_node(node, type_hierarchy, load_default_profiles())

    assert active_profiles.profile_ids == ("generic",)
    assert not has_profile_override_for_node(node, active_profiles, type_definition_hierarchy=type_hierarchy)


# --- Machinery profile must not treat foreign type names as relationships ------------------
# ``DataItemType`` is a Core VariableType (OPC 10000-8 §5.3.1), not a Machinery
# reference type, and ``FlowsTo`` belongs to the TMC model (OPC 30060 §12.1). Neither
# may steer the relationship class of a Machinery-classified node.

_DEFAULT_MACHINERY_PROFILE = load_default_profiles()[0]


def test_machinery_profile_does_not_treat_core_variable_type_names_as_relationships() -> None:
    classification, confidence = classify_opcua_reference_with_confidence(
        reference_type_node_id="ns=0;i=2365",
        reference_browse_name="DataItemType",
        supertype_browse_names=["NonHierarchicalReferences"],
        target_node_class="Object",
        profiles=(_DEFAULT_MACHINERY_PROFILE,),
    )
    assert classification == "graph"
    assert confidence == "high"


# --- HasAddIn keeps the generic target-class-aware mapping ---------------------------------
# Machinery references AddIns with ``HasAddIn`` (OPC 40001-1 §9.2, §11.2), a Core
# ``HasComponent`` subtype (OPC 10000-5 §11.21). No Machinery-specific relationship rule
# is declared: object AddIns stay hierarchy (the machine->machine structural plane),
# Variable targets stay composition.

def test_machinery_profile_keeps_hasaddin_hierarchy_for_object_targets() -> None:
    classification, confidence = classify_opcua_reference_with_confidence(
        reference_type_node_id="ns=0;i=17604",
        reference_browse_name="HasAddIn",
        supertype_browse_names=["HasComponent", "Aggregates", "HasChild", "HierarchicalReferences"],
        target_node_class="Object",
        profiles=(_DEFAULT_MACHINERY_PROFILE,),
    )
    assert classification == "hierarchy"
    assert confidence == "high"


def test_machinery_profile_keeps_hasaddin_composition_for_variable_targets() -> None:
    classification, confidence = classify_opcua_reference_with_confidence(
        reference_type_node_id="ns=0;i=17604",
        reference_browse_name="HasAddIn",
        supertype_browse_names=["HasComponent", "Aggregates", "HasChild", "HierarchicalReferences"],
        target_node_class="Variable",
        profiles=(_DEFAULT_MACHINERY_PROFILE,),
    )
    assert classification == "composition"
    assert confidence == "medium"


# --- Machinery overrides are scoped to their namespace identity ----------------------------

def test_machinery_override_applies_to_identification_subtypes_via_machinery_ancestry() -> None:
    """Intentionally broad rule, scoped to the Machinery namespace.

    Concrete identification subtypes (e.g. a vendor ``MachineIdentificationType``
    derived from the Machinery ``MachineryItemIdentificationType``, OPC 40001-1 §8.1)
    inherit the Machinery identity through their ancestry and receive the asset role
    with the AddIn structural plane forced to hierarchy. Types that merely inherit the
    DI ``FunctionalGroupType`` without a Machinery-namespace entry are not covered.
    """
    node = _collision_object("ns=1;s=Nameplate", "Nameplate")
    type_hierarchy = (
        OpcUaTypeDefinitionInfo(
            node_id="ns=2;i=3000",
            browse_name="MachineIdentificationType",
            namespace_uri="urn:vendor:types",
        ),
        OpcUaTypeDefinitionInfo(
            node_id="nsu=http://opcfoundation.org/UA/Machinery/;i=1004",
            browse_name="MachineryItemIdentificationType",
            namespace_uri="http://opcfoundation.org/UA/Machinery/",
        ),
    )

    active_profiles = active_profiles_for_node(node, type_hierarchy, load_default_profiles())

    assert active_profiles.profile_ids == ("machinery", "generic")
    assert has_profile_override_for_node(node, active_profiles, type_definition_hierarchy=type_hierarchy)
    assert forced_relationship_class_for_node(node, active_profiles, type_hierarchy) == "hierarchy"
    assert resolve_semantic_role(node, active_profiles, type_definition_hierarchy=type_hierarchy) == "asset"


def test_machinery_override_does_not_match_same_type_name_in_other_namespace() -> None:
    """Namespace-scoped overrides only apply to chain entries in the override namespace."""
    node = _collision_object("ns=1;s=Nameplate", "Nameplate")
    type_hierarchy = (
        OpcUaTypeDefinitionInfo(
            node_id="ns=2;i=3000",
            browse_name="MachineryItemIdentificationType",
            namespace_uri="urn:unrelated:types",
        ),
    )

    active_profiles = active_profiles_for_node(node, type_hierarchy, load_default_profiles())

    assert active_profiles.profile_ids == ("generic",)
    assert not has_profile_override_for_node(node, active_profiles, type_definition_hierarchy=type_hierarchy)
    assert forced_relationship_class_for_node(node, active_profiles, type_hierarchy) is None


def test_core_data_item_type_is_not_a_machinery_override() -> None:
    """``DataItemType`` (OPC 10000-8 §5.3.1) carries no Machinery override semantics."""
    node = _collision_object("ns=1;s=Sensor", "Sensor")
    active_profiles = ResolvedProfileSet(profiles=load_default_profiles())
    type_hierarchy = (
        OpcUaTypeDefinitionInfo(
            node_id="ns=0;i=2365",
            browse_name="DataItemType",
            namespace_uri="http://opcfoundation.org/UA/",
        ),
    )

    assert forced_relationship_class_for_node(node, active_profiles, type_definition_hierarchy=type_hierarchy) is None
    assert not has_profile_override_for_node(node, active_profiles, type_definition_hierarchy=type_hierarchy)


# --- Custom profile reference rules must dictate the relationship class ------------------
# A profile-declared composition alias has no built-in HasComponent/HasProperty lineage, so
# the shared family classifier used to fall through to "hierarchy" while still reporting high
# confidence. The matched profile category must drive the result instead.

_VENDOR_PROFILE = SemanticProfile(
    profile_id="vendor",
    priority=20,
    namespace_uri="",
    hierarchy_references=("VendorOrganizes",),
    composition_references=("VendorComposedBy",),
    graph_references=("VendorFlowsTo",),
)


def test_profile_custom_composition_alias_classifies_composition_for_object_target() -> None:
    classification, confidence = classify_opcua_reference_with_confidence(
        reference_type_node_id="ns=2;i=6000",
        reference_browse_name="VendorComposedBy",
        supertype_browse_names=["NonHierarchicalReferences"],
        target_node_class="Object",
        profiles=(_VENDOR_PROFILE,),
    )
    assert classification == "composition"
    assert confidence == "medium"


def test_profile_custom_composition_alias_classifies_composition_for_variable_target() -> None:
    classification, confidence = classify_opcua_reference_with_confidence(
        reference_type_node_id="ns=2;i=6000",
        reference_browse_name="VendorComposedBy",
        supertype_browse_names=["NonHierarchicalReferences"],
        target_node_class="Variable",
        profiles=(_VENDOR_PROFILE,),
    )
    assert classification == "composition"
    assert confidence == "medium"


def test_profile_custom_hierarchy_alias_classifies_hierarchy_for_variable_target() -> None:
    classification, confidence = classify_opcua_reference_with_confidence(
        reference_type_node_id="ns=2;i=6001",
        reference_browse_name="VendorOrganizes",
        supertype_browse_names=["NonHierarchicalReferences"],
        target_node_class="Variable",
        profiles=(_VENDOR_PROFILE,),
    )
    assert classification == "hierarchy"
    assert confidence == "high"


def test_profile_custom_graph_alias_classifies_graph() -> None:
    classification, confidence = classify_opcua_reference_with_confidence(
        reference_type_node_id="ns=2;i=6002",
        reference_browse_name="VendorFlowsTo",
        supertype_browse_names=["NonHierarchicalReferences"],
        target_node_class="Object",
        profiles=(_VENDOR_PROFILE,),
    )
    assert classification == "graph"
    assert confidence == "high"


# --- Overrides must match type identity, not instance names ------------------------------

def _collision_object(node_id: str, browse_name: str) -> OpcUaNodeInfo:
    return OpcUaNodeInfo(
        node_id=node_id,
        parent_node_id=None,
        browse_name=browse_name,
        display_name=browse_name,
        node_class="Object",
        data_type=None,
        type_definition_id="ns=1;i=999",
    )


def test_semantic_role_override_requires_type_ancestry_not_instance_browse_name() -> None:
    """An instance named like an override type but lacking that type must stay the default."""
    node = _collision_object("ns=1;s=FunctionalGroupType", "FunctionalGroupType")
    active_profiles = ResolvedProfileSet(profiles=load_default_profiles())

    assert (
        resolve_semantic_role(node, active_profiles, type_definition_hierarchy=())
        == "asset"
    )
    assert not has_profile_override_for_node(node, active_profiles, type_definition_hierarchy=())


def test_forced_relationship_override_requires_type_ancestry_not_instance_browse_name() -> None:
    node = _collision_object("ns=1;s=MachineryItemIdentificationType", "MachineryItemIdentificationType")
    active_profiles = ResolvedProfileSet(profiles=load_default_profiles())

    assert forced_relationship_class_for_node(
        node, active_profiles, type_definition_hierarchy=()
    ) is None


def test_forced_relationship_override_applies_from_type_ancestry() -> None:
    """The forced-relationship mechanism stays available for custom profiles."""
    vendor_profile = SemanticProfile(
        profile_id="vendor",
        priority=20,
        namespace_uri="",
        overrides=(ProfileOverride(type_name="VendorItemType", force_hierarchy=True),),
    )
    node = _collision_object("ns=1;s=Sensor", "Sensor")
    active_profiles = ResolvedProfileSet(profiles=(vendor_profile,))
    type_hierarchy = (
        OpcUaTypeDefinitionInfo(
            node_id="ns=3;i=1001",
            browse_name="VendorItemType",
            namespace_uri="urn:vendor:types",
        ),
    )

    assert forced_relationship_class_for_node(
        node, active_profiles, type_definition_hierarchy=type_hierarchy
    ) == "hierarchy"


def test_namespace_scoped_override_requires_namespace_and_browse_name_on_one_entry() -> None:
    """An override with a namespace matches only chain entries in that namespace."""
    scoped_profile = SemanticProfile(
        profile_id="scoped",
        priority=20,
        namespace_uri="",
        overrides=(
            ProfileOverride(
                type_name="ItemType",
                namespace_uri="urn:vendor:types",
                semantic_role="asset",
                force_hierarchy=True,
            ),
        ),
    )
    node = _collision_object("ns=1;s=Widget", "Widget")
    matching_hierarchy = (
        OpcUaTypeDefinitionInfo(
            node_id="ns=4;i=2",
            browse_name="ItemType",
            namespace_uri="urn:vendor:types",
        ),
    )
    unrelated_hierarchy = (
        OpcUaTypeDefinitionInfo(
            node_id="ns=5;i=1",
            browse_name="ItemType",
            namespace_uri="urn:other:types",
        ),
    )

    active_profiles = active_profiles_for_node(node, matching_hierarchy, (scoped_profile,))
    assert has_profile_override_for_node(node, active_profiles, matching_hierarchy)
    assert forced_relationship_class_for_node(node, active_profiles, matching_hierarchy) == "hierarchy"

    active_profiles = active_profiles_for_node(node, unrelated_hierarchy, (scoped_profile,))
    assert not has_profile_override_for_node(node, active_profiles, unrelated_hierarchy)
    assert forced_relationship_class_for_node(node, active_profiles, unrelated_hierarchy) is None


def test_profile_namespace_and_known_type_constraints_match_on_separate_entries() -> None:
    """Namespace and known-type constraints are evaluated independently per entry."""
    profile = SemanticProfile(
        profile_id="vendor-di",
        priority=20,
        namespace_uri="urn:vendor:types",
        known_types=("WidgetType",),
    )
    node = _collision_object("ns=1;s=Widget", "Widget")
    type_hierarchy = (
        OpcUaTypeDefinitionInfo(
            node_id="ns=5;i=1",
            browse_name="WidgetType",
            namespace_uri="urn:other:types",
        ),
        OpcUaTypeDefinitionInfo(
            node_id="ns=4;i=2",
            browse_name="VendorBaseType",
            namespace_uri="urn:vendor:types",
        ),
    )

    active_profiles = active_profiles_for_node(node, type_hierarchy, (profile,))

    assert active_profiles.profile_ids == ("vendor-di",)


# --- Explicit empty-profile contract: None defaults, () means none -----------------------

def _plain_container() -> OpcUaNodeInfo:
    return OpcUaNodeInfo(
        node_id="ns=1;s=Container",
        parent_node_id=None,
        browse_name="Container",
        display_name="Container",
        node_class="Object",
        data_type=None,
        type_definition_id=None,
    )


def test_active_profiles_none_uses_defaults() -> None:
    assert active_profiles_for_node(_plain_container(), ()).profile_ids == ("generic",)


def test_active_profiles_empty_tuple_means_no_profiles() -> None:
    assert active_profiles_for_node(_plain_container(), (), ()).profile_ids == ()


# --- Precedence for equal-priority profiles and conflicting overrides --------------------

def test_active_profiles_equal_priority_preserves_tuple_order() -> None:
    first = SemanticProfile(profile_id="first", priority=5, namespace_uri="")
    second = SemanticProfile(profile_id="second", priority=5, namespace_uri="")

    active_profiles = active_profiles_for_node(_plain_container(), (), (first, second))
    assert active_profiles.profile_ids == ("first", "second")


def test_conflicting_overrides_higher_priority_profile_wins() -> None:
    high = SemanticProfile(
        profile_id="high",
        priority=100,
        namespace_uri="",
        overrides=(ProfileOverride(type_name="WidgetType", semantic_role="asset"),),
    )
    low = SemanticProfile(
        profile_id="low",
        priority=1,
        namespace_uri="",
        overrides=(ProfileOverride(type_name="WidgetType", semantic_role="datapoint"),),
    )
    node = _collision_object("ns=1;s=Widget", "Widget")
    type_hierarchy = (OpcUaTypeDefinitionInfo(node_id="ns=1;i=500", browse_name="WidgetType"),)

    active_profiles = active_profiles_for_node(node, type_hierarchy, (low, high))
    assert active_profiles.profile_ids == ("high", "low")
    assert resolve_semantic_role(node, active_profiles, type_definition_hierarchy=type_hierarchy) == "asset"


def test_conflicting_overrides_equal_priority_tuple_order_wins() -> None:
    first = SemanticProfile(
        profile_id="first",
        priority=10,
        namespace_uri="",
        overrides=(ProfileOverride(type_name="WidgetType", semantic_role="asset"),),
    )
    second = SemanticProfile(
        profile_id="second",
        priority=10,
        namespace_uri="",
        overrides=(ProfileOverride(type_name="WidgetType", semantic_role="datapoint"),),
    )
    node = _collision_object("ns=1;s=Widget", "Widget")
    type_hierarchy = (OpcUaTypeDefinitionInfo(node_id="ns=1;i=500", browse_name="WidgetType"),)

    active_profiles = active_profiles_for_node(node, type_hierarchy, (first, second))
    assert active_profiles.profile_ids == ("first", "second")
    assert resolve_semantic_role(node, active_profiles, type_definition_hierarchy=type_hierarchy) == "asset"
