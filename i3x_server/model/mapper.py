from __future__ import annotations

import hashlib
from typing import Literal

from i3x_server.domain.ports.opcua import OpcUaNodeInfo
from i3x_server.model.semantic_profiles import (
    MappingConfidence,
    SemanticProfile,
    SemanticRole,
)
from i3x_server.schemas.i3x import ModelNode, NodeKind

CLASS_TO_KIND: dict[str, NodeKind] = {
    "Object": "asset",
    "Variable": "property",
    "Method": "action",
}

ReferenceClass = Literal["hierarchy", "composition", "graph", "type-meta", "ignore"]

_HIERARCHY_REFERENCE_NAMES = {
    "organizes",
    "haschild",
    "hierarchicalreferences",
}

_COMPOSITION_REFERENCE_NAMES = {
    "hascomponent",
    "hasorderedcomponent",
    "hasproperty",
    "propertyof",
}

_TYPE_META_REFERENCE_NAMES = {
    "hastypedefinition",
    "hassubtype",
}

_GRAPH_REFERENCE_NAMES = {
    "nonhierarchicalreferences",
}

# OPC UA standard reference type node IDs (namespace 0)
# These use normalized form (alphanumeric only, matching _normalize_token output)
_HIERARCHY_REFERENCE_NODE_IDS = {
    "i35",  # Organizes (ns=0;i=35)
    "i33",  # HierarchicalReferences (ns=0;i=33)
}

_COMPOSITION_REFERENCE_NODE_IDS = {
    "i47",  # HasComponent (ns=0;i=47)
    "i48",  # HasOrderedComponent (ns=0;i=48)
    "i46",  # HasProperty (ns=0;i=46)
}

_TYPE_META_REFERENCE_NODE_IDS = {
    "i40",  # HasTypeDefinition (ns=0;i=40)
    "i45",  # HasSubtype (ns=0;i=45)
}

_GRAPH_REFERENCE_NODE_IDS = {
    "i31",  # References (base for non-hierarchical) (ns=0;i=31)
    "i32",  # NonHierarchicalReferences (ns=0;i=32)
}


def _classify_hierarchical_family(tokens: set[str], target_node_class: str | None) -> ReferenceClass:
    target_class_known = isinstance(target_node_class, str)
    target_is_variable = target_class_known and target_node_class == "Variable"

    has_property_lineage = bool(tokens & {"hasproperty", "propertyof", "i46"})
    has_component_lineage = bool(tokens & {"hascomponent", "hasorderedcomponent", "i47", "i48"})

    if has_property_lineage:
        return "composition"
    if has_component_lineage:
        # HasComponent-style relationships become hierarchy for non-Variable targets.
        # This keeps machine->machine as i3X hierarchy while machine->variable is composition.
        if target_class_known and not target_is_variable:
            return "hierarchy"
        return "composition"
    return "hierarchy"


def _normalize_token(value: str | None) -> str:
    if not value:
        return ""
    lowered = value.strip().lower()
    if not lowered:
        return ""
    if ":" in lowered:
        lowered = lowered.rsplit(":", 1)[-1]
    if "/" in lowered:
        lowered = lowered.rsplit("/", 1)[-1]
    if ";" in lowered:
        lowered = lowered.rsplit(";", 1)[-1]
    return "".join(ch for ch in lowered if ch.isalnum())


def _reference_class_from_profile_rules(
    tokens: set[str],
    profiles: tuple[SemanticProfile, ...] | None,
) -> ReferenceClass | None:
    if not profiles:
        return None
    for profile in profiles:
        hierarchy_tokens = {_normalize_token(name) for name in profile.hierarchy_references}
        composition_tokens = {_normalize_token(name) for name in profile.composition_references}
        graph_tokens = {_normalize_token(name) for name in profile.graph_references}
        if tokens & hierarchy_tokens:
            return "hierarchy"
        if tokens & composition_tokens:
            return "composition"
        if tokens & graph_tokens:
            return "graph"
    return None


def classify_opcua_reference(
    reference_type_node_id: str | None,
    reference_browse_name: str | None,
    supertype_browse_names: list[str] | None = None,
    target_node_class: str | None = None,
) -> ReferenceClass:
    # Build a normalized view of the full reference-type lineage.
    tokens = {
        _normalize_token(reference_type_node_id),
        _normalize_token(reference_browse_name),
    }
    if supertype_browse_names:
        tokens.update(_normalize_token(item) for item in supertype_browse_names)
    tokens.discard("")

    if tokens & _TYPE_META_REFERENCE_NAMES:
        return "type-meta"
    if tokens & _TYPE_META_REFERENCE_NODE_IDS:
        return "type-meta"

    # Ancestry-root-first classification:
    # NonHierarchicalReferences wins if both roots appear in a malformed lineage.
    has_nonhierarchical_root = bool(tokens & (_GRAPH_REFERENCE_NAMES | {"i32"}))
    has_hierarchical_root = bool(tokens & (_HIERARCHY_REFERENCE_NAMES | {"i33"}))

    if has_nonhierarchical_root:
        return "graph"
    if has_hierarchical_root:
        return _classify_hierarchical_family(tokens, target_node_class)

    # Compatibility fallback when ancestry roots are unavailable.
    if tokens & _TYPE_META_REFERENCE_NAMES:
        return "type-meta"
    if tokens & _COMPOSITION_REFERENCE_NAMES:
        return _classify_hierarchical_family(tokens, target_node_class)
    if tokens & _COMPOSITION_REFERENCE_NODE_IDS:
        return _classify_hierarchical_family(tokens, target_node_class)
    if tokens & _HIERARCHY_REFERENCE_NAMES:
        return "hierarchy"
    if tokens & _HIERARCHY_REFERENCE_NODE_IDS:
        return "hierarchy"
    if tokens & _GRAPH_REFERENCE_NAMES:
        return "graph"
    if tokens & _GRAPH_REFERENCE_NODE_IDS:
        return "graph"
    return "graph"


def classify_opcua_reference_with_confidence(
    reference_type_node_id: str | None,
    reference_browse_name: str | None,
    supertype_browse_names: list[str] | None = None,
    target_node_class: str | None = None,
    profiles: tuple[SemanticProfile, ...] | None = None,
) -> tuple[ReferenceClass, MappingConfidence]:
    tokens = {
        _normalize_token(reference_type_node_id),
        _normalize_token(reference_browse_name),
    }
    if supertype_browse_names:
        tokens.update(_normalize_token(item) for item in supertype_browse_names)
    tokens.discard("")

    has_nonhierarchical_root = bool(tokens & (_GRAPH_REFERENCE_NAMES | {"i32"}))
    has_hierarchical_root = bool(tokens & (_HIERARCHY_REFERENCE_NAMES | {"i33"}))
    if has_nonhierarchical_root and has_hierarchical_root:
        classification = classify_opcua_reference(
            reference_type_node_id=reference_type_node_id,
            reference_browse_name=reference_browse_name,
            supertype_browse_names=supertype_browse_names,
            target_node_class=target_node_class,
        )
        if classification == "composition":
            return classification, "medium"
        return classification, "high"

    profile_class = _reference_class_from_profile_rules(tokens, profiles)
    if profile_class is not None:
        return profile_class, "high"

    classification = classify_opcua_reference(
        reference_type_node_id=reference_type_node_id,
        reference_browse_name=reference_browse_name,
        supertype_browse_names=supertype_browse_names,
        target_node_class=target_node_class,
    )
    if classification == "composition":
        return classification, "medium"
    return classification, "high"


def stable_i3x_id(node_id: str, kind: NodeKind) -> str:
    digest = hashlib.sha1(node_id.encode("utf-8")).hexdigest()[:16]
    return f"{kind}-{digest}"


def infer_kind(node: OpcUaNodeInfo) -> NodeKind:
    if node.event_notifier:
        return "eventSource"
    return CLASS_TO_KIND.get(node.node_class, "asset")


def map_type(node: OpcUaNodeInfo, kind: NodeKind) -> str | None:
    if kind == "property":
        return node.data_type
    return None


def map_node(
    node: OpcUaNodeInfo,
    children: list[str],
    *,
    parent_id: str | None = None,
    is_composition: bool = False,
    semantic_role: SemanticRole = "unknown",
    mapping_confidence: MappingConfidence = "medium",
    relationships: dict[str, list[str]] | None = None,
    metadata: dict[str, object] | None = None,
) -> ModelNode:
    kind = infer_kind(node)
    return ModelNode(
        id=stable_i3x_id(node.node_id, kind),
        name=node.display_name or node.browse_name,
        kind=kind,
        type=map_type(node, kind),
        children=children,
        source_node_id=node.node_id,
        source_type_id=node.type_definition_id,
        parent_id=parent_id,
        is_composition=is_composition,
        semantic_role=semantic_role,
        mapping_confidence=mapping_confidence,
        relationships=relationships or {},
        metadata=metadata or {},
    )
