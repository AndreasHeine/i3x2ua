from __future__ import annotations

import re
from typing import Any

from i3x_server.api.v1.contracts import (
    Namespace,
    ObjectInstanceMetadata,
    ObjectInstanceResponse,
    ObjectTypeResponse,
    RelatedObjectResult,
    RelationshipType,
)
from i3x_server.application.ports.opcua import OpcUaNamespaceInfo, OpcUaObjectTypeInfo
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.objecttype_schema import build_object_type_schema
from i3x_server.schemas.state import BuildResult

_I3X_NAMESPACE = "https://cesmii.org/i3x"
_OPCUA_NAMESPACE = "https://opcfoundation.org/UA/"


def _normalize_namespace_uri(uri: str) -> str:
    return uri.strip().rstrip("/").lower()


def _canonical_namespace_uri(uri: str, namespace_infos: list[OpcUaNamespaceInfo]) -> str:
    normalized = _normalize_namespace_uri(uri)
    for item in namespace_infos:
        if _normalize_namespace_uri(item.uri) == normalized:
            return item.uri
    return uri


def _namespace_uri_for_node_id(node_id: str, namespace_infos: list[OpcUaNamespaceInfo]) -> str:
    match = re.search(r"ns=(\d+)", node_id)
    namespace_index = int(match.group(1)) if match is not None else 0
    if 0 <= namespace_index < len(namespace_infos):
        return namespace_infos[namespace_index].uri
    return ""


def _expanded_node_id(node_id: str, namespace_infos: list[OpcUaNamespaceInfo]) -> str:
    if node_id.startswith("nsu="):
        return node_id

    match = re.match(r"^(?:ns=(\d+);)?([isgb]=.+)$", node_id)
    if match is None:
        return node_id

    namespace_index = int(match.group(1)) if match.group(1) is not None else 0
    identifier = match.group(2)

    if namespace_index == 0:
        return f"nsu=http://opcfoundation.org/UA/;{identifier}"

    if not (0 <= namespace_index < len(namespace_infos)):
        return node_id

    namespace_uri = namespace_infos[namespace_index].uri
    if not namespace_uri:
        return node_id

    return f"nsu={namespace_uri};{identifier}"


def _namespace_uri_from_expanded_node_id(node_id: str) -> str | None:
    match = re.match(r"^nsu=([^;]+);", node_id)
    if match is None:
        return None
    namespace_uri = match.group(1)
    return namespace_uri or None


def _is_null_opcua_type_node_id(node_id: str) -> bool:
    normalized = node_id.strip()
    if re.match(r"^nsu=[^;]+;i=0$", normalized, flags=re.IGNORECASE):
        return True
    if re.match(r"^ns=\d+;i=0$", normalized, flags=re.IGNORECASE):
        return True
    return bool(re.match(r"^i=0$", normalized, flags=re.IGNORECASE))


def _to_namespace(item: OpcUaNamespaceInfo) -> Namespace:
    display_name = item.display_name or _display_name_for_uri(item.uri)
    return Namespace(uri=item.uri, displayName=display_name)


def _display_name_for_uri(uri: str) -> str:
    parsed_path = uri.split("//", 1)[-1]
    tail = parsed_path.rsplit("/", 1)[-1] if "/" in parsed_path else parsed_path
    token = tail.replace("-", " ").replace("_", " ")
    if token:
        if any(ch.isdigit() for ch in token):
            return token.upper()
        return token.title()
    host = uri.split("//", 1)[-1].split(":", 1)[0].split(".")
    return host[0].title() if host and host[0] else uri


def _to_element_id(name: str) -> str:
    normalized = re.sub(r"Type$", "-type", name)
    split = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", normalized)
    lowered = split.replace("_", "-").lower()
    compact = re.sub(r"-+", "-", lowered).strip("-")
    return compact or "unknown-type"


def _to_urn_token(value: str) -> str:
    lowered = value.lower()
    token = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return token or "unknown"


def _object_type_element_id(
    item: OpcUaObjectTypeInfo,
    namespace_uri: str,
) -> str:
    return ":".join(
        [
            "urn",
            "opcua",
            "objecttype",
            _to_urn_token(namespace_uri),
            _to_urn_token(item.browse_name),
            _to_urn_token(item.node_id),
        ]
    )


def _virtual_object_type_element_id(
    namespace_uri: str,
    display_name: str,
    source_type_id: str,
) -> str:
    return ":".join(
        [
            "urn",
            "opcua",
            "objecttype",
            _to_urn_token(namespace_uri),
            _to_urn_token(display_name),
            _to_urn_token(source_type_id),
        ]
    )


def _object_type_element_ids_by_node_id(
    object_types: list[OpcUaObjectTypeInfo],
    namespace_infos: list[OpcUaNamespaceInfo],
) -> dict[str, str]:
    return {
        item.node_id: _object_type_element_id(item, _namespace_uri_for_node_id(item.node_id, namespace_infos))
        for item in object_types
    }


def _relationship_type_items(model: BuildResult | None = None) -> list[RelationshipType]:
    items = [
        RelationshipType(
            elementId="HasParent",
            displayName="HasParent",
            namespaceUri=_I3X_NAMESPACE,
            relationshipId="HasParent",
            reverseOf="HasChildren",
        ),
        RelationshipType(
            elementId="HasChildren",
            displayName="HasChildren",
            namespaceUri=_I3X_NAMESPACE,
            relationshipId="HasChildren",
            reverseOf="HasParent",
        ),
        RelationshipType(
            elementId="HasComponent",
            displayName="HasComponent",
            namespaceUri=_I3X_NAMESPACE,
            relationshipId="HasComponent",
            reverseOf="ComponentOf",
        ),
        RelationshipType(
            elementId="ComponentOf",
            displayName="ComponentOf",
            namespaceUri=_I3X_NAMESPACE,
            relationshipId="ComponentOf",
            reverseOf="HasComponent",
        ),
    ]
    if model is not None:
        graph_names = getattr(model, "graph_relationship_names", None) or set()
        existing = {item.elementId for item in items}
        for name in sorted(graph_names):
            if name in existing:
                continue
            reverse_name = f"inverseOf_{name}"
            items.append(
                RelationshipType(
                    elementId=name,
                    displayName=name,
                    namespaceUri=_OPCUA_NAMESPACE,
                    relationshipId=name,
                    reverseOf=reverse_name,
                )
            )
            items.append(
                RelationshipType(
                    elementId=reverse_name,
                    displayName=reverse_name,
                    namespaceUri=_OPCUA_NAMESPACE,
                    relationshipId=reverse_name,
                    reverseOf=name,
                )
            )
    return items


def _find_model_node(model: BuildResult, element_id: str) -> ModelNode | None:
    node = model.nodes_by_id.get(element_id)
    if node is not None:
        return node

    raw_node_id_by_name = getattr(model, "node_id_by_name", None)
    if isinstance(raw_node_id_by_name, dict):
        indexed_name_id = raw_node_id_by_name.get(element_id)
        if isinstance(indexed_name_id, str):
            indexed_name_node = model.nodes_by_id.get(indexed_name_id)
            if indexed_name_node is not None:
                return indexed_name_node

    raw_node_id_by_type = getattr(model, "node_id_by_type", None)
    if isinstance(raw_node_id_by_type, dict):
        indexed_type_id = raw_node_id_by_type.get(element_id)
        if isinstance(indexed_type_id, str):
            indexed_type_node = model.nodes_by_id.get(indexed_type_id)
            if indexed_type_node is not None:
                return indexed_type_node

    for candidate in model.nodes_by_id.values():
        if candidate.name == element_id:
            return candidate
        if candidate.type == element_id:
            return candidate
        if candidate.source_node_id == element_id:
            return candidate
        if candidate.source_node_id.lower() == element_id.lower():
            return candidate
    return None


def _parent_id_for_node(model: BuildResult, node_id: str) -> str | None:
    if node_id in model.root_ids:
        return None

    raw_hierarchy_parent_by_id = getattr(model, "hierarchy_parent_by_id", None)
    if isinstance(raw_hierarchy_parent_by_id, dict):
        if node_id in raw_hierarchy_parent_by_id:
            indexed_parent = raw_hierarchy_parent_by_id.get(node_id)
            if isinstance(indexed_parent, str):
                return indexed_parent
        else:
            node = model.nodes_by_id.get(node_id)
            if node and node.kind in {"asset", "eventSource"}:
                return None

    raw_parent_by_id = getattr(model, "parent_by_id", None)
    if isinstance(raw_parent_by_id, dict):
        indexed_parent = raw_parent_by_id.get(node_id)
        if isinstance(indexed_parent, str):
            return indexed_parent

    raw_hierarchy_children_by_id = getattr(model, "hierarchy_children_by_id", None)
    if isinstance(raw_hierarchy_children_by_id, dict):
        for parent_id, child_ids in raw_hierarchy_children_by_id.items():
            if not isinstance(parent_id, str) or not isinstance(child_ids, list):
                continue
            if node_id in child_ids:
                return parent_id

    for parent_id, child_ids in model.children_by_id.items():
        if node_id in child_ids:
            return parent_id
    return None


def _hierarchy_children_for_node(model: BuildResult, node: ModelNode) -> list[str]:
    raw_hierarchy_children_by_id = getattr(model, "hierarchy_children_by_id", None)
    if isinstance(raw_hierarchy_children_by_id, dict) and node.id in raw_hierarchy_children_by_id:
        children = raw_hierarchy_children_by_id.get(node.id, [])
        if isinstance(children, list):
            return [child_id for child_id in children if isinstance(child_id, str)]

    return [
        child_id
        for child_id in model.children_by_id.get(node.id, [])
        if (model.nodes_by_id.get(child_id) is not None and model.nodes_by_id[child_id].kind != "property")
    ]


def _composition_children_for_node(model: BuildResult, node: ModelNode) -> list[str]:
    raw_composition_children_by_id = getattr(model, "composition_children_by_id", None)
    if isinstance(raw_composition_children_by_id, dict) and node.id in raw_composition_children_by_id:
        children = raw_composition_children_by_id.get(node.id, [])
        if isinstance(children, list):
            return [child_id for child_id in children if isinstance(child_id, str)]

    return [
        child_id
        for child_id in model.children_by_id.get(node.id, [])
        if (model.nodes_by_id.get(child_id) is not None and model.nodes_by_id[child_id].kind == "property")
    ]


def _relationships_for_node(model: BuildResult, node: ModelNode) -> dict[str, list[str]]:
    raw_relationships_by_id = getattr(model, "relationships_by_id", None)
    if isinstance(raw_relationships_by_id, dict):
        raw_for_node = raw_relationships_by_id.get(node.id)
        if isinstance(raw_for_node, dict) and raw_for_node:
            normalized: dict[str, list[str]] = {}
            for relationship_name, targets in raw_for_node.items():
                if not isinstance(relationship_name, str):
                    continue
                if isinstance(targets, list):
                    normalized_targets = [item for item in targets if isinstance(item, str)]
                elif isinstance(targets, str):
                    normalized_targets = [targets]
                else:
                    normalized_targets = []
                if normalized_targets:
                    normalized[relationship_name] = normalized_targets
            if normalized:
                return normalized

    parent_id = _parent_id_for_node(model, node.id)
    relationships: dict[str, list[str]] = {}
    if parent_id is not None:
        relationships["HasParent"] = [parent_id]
    hierarchy_children = _hierarchy_children_for_node(model, node)
    if hierarchy_children:
        relationships["HasChildren"] = hierarchy_children
    composition_children = _composition_children_for_node(model, node)
    if composition_children:
        relationships["HasComponent"] = composition_children
    return relationships


def _relationship_type_to_parent(node: ModelNode) -> RelationshipType:
    if node.kind == "property":
        return RelationshipType(
            elementId="ComponentOf",
            displayName="ComponentOf",
            namespaceUri=_I3X_NAMESPACE,
            relationshipId="ComponentOf",
            reverseOf="HasComponent",
        )
    return RelationshipType(
        elementId="HasParent",
        displayName="HasParent",
        namespaceUri=_I3X_NAMESPACE,
        relationshipId="HasParent",
        reverseOf="HasChildren",
    )


def _relationship_type_for_name(name: str, node: ModelNode) -> RelationshipType:
    if name == "HasChildren":
        return RelationshipType(
            elementId="HasChildren",
            displayName="HasChildren",
            namespaceUri=_I3X_NAMESPACE,
            relationshipId="HasChildren",
            reverseOf="HasParent",
        )
    if name == "HasParent":
        return _relationship_type_to_parent(node)
    if name == "HasComponent":
        return RelationshipType(
            elementId="HasComponent",
            displayName="HasComponent",
            namespaceUri=_I3X_NAMESPACE,
            relationshipId="HasComponent",
            reverseOf="ComponentOf",
        )
    if name == "ComponentOf":
        return RelationshipType(
            elementId="ComponentOf",
            displayName="ComponentOf",
            namespaceUri=_I3X_NAMESPACE,
            relationshipId="ComponentOf",
            reverseOf="HasComponent",
        )
    return RelationshipType(
        elementId=name,
        displayName=name,
        namespaceUri=_I3X_NAMESPACE,
        relationshipId=name,
        reverseOf="",
    )


def _resolve_type_namespace_uri(
    type_element_id: str,
    source_type_id_expanded: str,
    namespace_infos: list[OpcUaNamespaceInfo],
) -> str | None:
    type_namespace_uri = _namespace_uri_from_expanded_node_id(type_element_id)
    if type_namespace_uri is None:
        resolved_type_namespace_uri = _namespace_uri_for_node_id(type_element_id, namespace_infos)
        if resolved_type_namespace_uri:
            type_namespace_uri = resolved_type_namespace_uri
    if type_namespace_uri is None:
        type_namespace_uri = _namespace_uri_from_expanded_node_id(source_type_id_expanded)
    if type_namespace_uri is None:
        resolved_source_namespace_uri = _namespace_uri_for_node_id(type_element_id.split(":")[0], namespace_infos)
        type_namespace_uri = resolved_source_namespace_uri or None
    if type_namespace_uri is not None:
        type_namespace_uri = _canonical_namespace_uri(type_namespace_uri, namespace_infos)
    return type_namespace_uri


def _build_object_instance_metadata(
    model: BuildResult,
    node: ModelNode,
    type_namespace_uri: str | None,
    source_type_id_expanded: str,
) -> ObjectInstanceMetadata | None:
    relationships: dict[str, Any] = {}
    normalized_relationships = _relationships_for_node(model, node)
    for relationship_name, targets in normalized_relationships.items():
        if relationship_name == "HasParent":
            relationships[relationship_name] = targets[0]
        else:
            relationships[relationship_name] = targets

    composition_parent_id: str | None = None
    raw_composition_parent_by_id = getattr(model, "composition_parent_by_id", None)
    if isinstance(raw_composition_parent_by_id, dict):
        indexed_composition_parent = raw_composition_parent_by_id.get(node.id)
        if isinstance(indexed_composition_parent, str):
            composition_parent_id = indexed_composition_parent

    return ObjectInstanceMetadata(
        typeNamespaceUri=type_namespace_uri,
        sourceTypeId=source_type_id_expanded,
        description=f"Derived from model node {node.name}",
        compositionParentId=composition_parent_id,
        relationships=relationships,
    )


def _unknown_type_element_id(namespace_infos: list[OpcUaNamespaceInfo]) -> str:
    namespace_uri = namespace_infos[0].uri if namespace_infos else "https://cesmii.org/i3x/unknown"
    namespace_uri = _canonical_namespace_uri(namespace_uri, namespace_infos) if namespace_infos else namespace_uri
    return _virtual_object_type_element_id(namespace_uri, "UnknownType", "nsu=http://opcfoundation.org/UA/;i=0")


def _resolved_type_element_id_for_node(
    node: ModelNode,
    namespace_infos: list[OpcUaNamespaceInfo],
    object_type_element_ids_by_node_id: dict[str, str],
    object_type_element_ids_by_source_type: dict[str, str],
) -> str:
    if node.kind == "property":
        raw_type_element_id = node.type or "unknown-type"
        type_element_id = _expanded_node_id(raw_type_element_id, namespace_infos)
        if _is_null_opcua_type_node_id(type_element_id):
            return _unknown_type_element_id(namespace_infos)
        return object_type_element_ids_by_source_type.get(type_element_id.lower(), type_element_id)

    source_type_id = node.source_type_id
    if not source_type_id:
        return _unknown_type_element_id(namespace_infos)

    source_type_id_expanded = _expanded_node_id(source_type_id, namespace_infos)
    if _is_null_opcua_type_node_id(source_type_id_expanded):
        return _unknown_type_element_id(namespace_infos)
    resolved = object_type_element_ids_by_node_id.get(source_type_id)
    if resolved is not None:
        return resolved
    return object_type_element_ids_by_source_type.get(source_type_id_expanded.lower(), source_type_id_expanded)


def _to_object_instance(
    model: BuildResult,
    node: ModelNode,
    include_metadata: bool,
    namespace_infos: list[OpcUaNamespaceInfo],
    object_type_element_ids_by_node_id: dict[str, str],
    object_type_element_ids_by_source_type: dict[str, str],
) -> ObjectInstanceResponse:
    source_type_id = node.source_type_id or node.source_node_id
    source_type_id_expanded = _expanded_node_id(source_type_id, namespace_infos)
    type_element_id = _resolved_type_element_id_for_node(
        node,
        namespace_infos,
        object_type_element_ids_by_node_id,
        object_type_element_ids_by_source_type,
    )

    type_namespace_uri = _resolve_type_namespace_uri(type_element_id, source_type_id_expanded, namespace_infos)

    metadata = None
    if include_metadata:
        metadata = _build_object_instance_metadata(model, node, type_namespace_uri, source_type_id_expanded)

    return ObjectInstanceResponse(
        elementId=node.id,
        displayName=node.name,
        typeElementId=type_element_id,
        parentId=_parent_id_for_node(model, node.id),
        isComposition=bool(_composition_children_for_node(model, node)),
        isExtended=False,
        metadata=metadata,
    )


def _object_type_related_instances(
    model: BuildResult,
    type_node_id: str,
    namespace_infos: list[OpcUaNamespaceInfo],
    element_ids_by_node_id: dict[str, str],
    element_ids_by_source_type: dict[str, str],
) -> list[ObjectInstanceResponse]:
    instances_by_type_id = getattr(model, "instances_by_type_id", {})
    related: list[ObjectInstanceResponse] = []
    for instance_id in instances_by_type_id.get(type_node_id, []):
        node = model.nodes_by_id.get(instance_id)
        if node is None:
            continue
        related.append(
            _to_object_instance(
                model,
                node,
                include_metadata=True,
                namespace_infos=namespace_infos,
                object_type_element_ids_by_node_id=element_ids_by_node_id,
                object_type_element_ids_by_source_type=element_ids_by_source_type,
            )
        )
    return related


def _to_object_type(
    item: OpcUaObjectTypeInfo,
    model: BuildResult,
    namespace_infos: list[OpcUaNamespaceInfo],
    object_types_by_node_id: dict[str, OpcUaObjectTypeInfo],
    element_ids_by_node_id: dict[str, str],
    element_ids_by_source_type: dict[str, str],
) -> ObjectTypeResponse:
    namespace_uri = _namespace_uri_for_node_id(item.node_id, namespace_infos)
    element_id = _object_type_element_id(item, namespace_uri)
    source_type_id = _expanded_node_id(item.node_id, namespace_infos)
    related_instances = _object_type_related_instances(
        model,
        item.node_id,
        namespace_infos,
        element_ids_by_node_id,
        element_ids_by_source_type,
    )
    return ObjectTypeResponse(
        elementId=element_id,
        displayName=item.display_name,
        namespaceUri=namespace_uri,
        sourceTypeId=source_type_id,
        schema=build_object_type_schema(item, object_types_by_node_id, element_ids_by_node_id, namespace_infos),
        related={"instances": related_instances} if related_instances else None,
    )


def _build_related_objects_for_node(
    model: BuildResult,
    node: ModelNode,
    relationship_type_filter: str | None,
    include_metadata: bool,
    namespace_infos: list[OpcUaNamespaceInfo],
    object_type_element_ids_by_node_id: dict[str, str],
    object_type_element_ids_by_source_type: dict[str, str],
) -> list[RelatedObjectResult]:
    related: list[RelatedObjectResult] = []
    relationship_map = _relationships_for_node(model, node)
    for relationship_name, target_ids in relationship_map.items():
        relationship = _relationship_type_for_name(relationship_name, node)
        if relationship_type_filter is not None and relationship.elementId != relationship_type_filter:
            continue
        for target_id in target_ids:
            target = model.nodes_by_id.get(target_id)
            if target is None:
                continue
            related.append(
                RelatedObjectResult(
                    sourceRelationship=relationship.displayName,
                    object=_to_object_instance(
                        model,
                        target,
                        include_metadata=include_metadata,
                        namespace_infos=namespace_infos,
                        object_type_element_ids_by_node_id=object_type_element_ids_by_node_id,
                        object_type_element_ids_by_source_type=object_type_element_ids_by_source_type,
                    ),
                )
            )
    return related
