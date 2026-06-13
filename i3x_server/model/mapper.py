from __future__ import annotations

import hashlib
from typing import Literal

from i3x_server.opcua.client import OpcUaNodeInfo
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
    "i35",       # Organizes (ns=0;i=35)
    "i33",       # HierarchicalReferences (ns=0;i=33)
}

_COMPOSITION_REFERENCE_NODE_IDS = {
    "i47",       # HasComponent (ns=0;i=47)
    "i48",       # HasOrderedComponent (ns=0;i=48)
    "i46",       # HasProperty (ns=0;i=46)
}

_TYPE_META_REFERENCE_NODE_IDS = {
    "i40",       # HasTypeDefinition (ns=0;i=40)
    "i45",       # HasSubtype (ns=0;i=45)
}

_GRAPH_REFERENCE_NODE_IDS = {
     "i31",       # References (base for non-hierarchical) (ns=0;i=31)
}


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


def classify_opcua_reference(
    reference_type_node_id: str | None,
    reference_browse_name: str | None,
    supertype_browse_names: list[str] | None = None,
) -> ReferenceClass:
    # Check node ID mappings first (most reliable - OPC UA std references)
    if reference_type_node_id:
        node_id_normalized = _normalize_token(reference_type_node_id)
        if node_id_normalized in _TYPE_META_REFERENCE_NODE_IDS:
            return "type-meta"
        if node_id_normalized in _COMPOSITION_REFERENCE_NODE_IDS:
            return "composition"
        if node_id_normalized in _HIERARCHY_REFERENCE_NODE_IDS:
            return "hierarchy"
        if node_id_normalized in _GRAPH_REFERENCE_NODE_IDS:
            return "graph"
    
    # Fall back to browse name matching (for custom reference types)
    tokens = {
        _normalize_token(reference_type_node_id),
        _normalize_token(reference_browse_name),
    }
    if supertype_browse_names:
        tokens.update(_normalize_token(item) for item in supertype_browse_names)
    tokens.discard("")

    if tokens & _TYPE_META_REFERENCE_NAMES:
        return "type-meta"
    if tokens & _COMPOSITION_REFERENCE_NAMES:
        return "composition"
    if tokens & _HIERARCHY_REFERENCE_NAMES:
        return "hierarchy"
    if tokens & _GRAPH_REFERENCE_NAMES:
        return "graph"
    return "graph"


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


def map_node(node: OpcUaNodeInfo, children: list[str]) -> ModelNode:
    kind = infer_kind(node)
    return ModelNode(
        id=stable_i3x_id(node.node_id, kind),
        name=node.display_name or node.browse_name,
        kind=kind,
        type=map_type(node, kind),
        children=children,
        source_node_id=node.node_id,
        source_type_id=node.type_definition_id,
    )
