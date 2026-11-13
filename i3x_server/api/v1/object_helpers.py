"""Presentation mapping from the built i3X model onto the v1 object/object-type payloads.

The helpers here translate ``BuildResult`` planes into the contract models (object instances,
object types, relationships, namespaces). Node and namespace arithmetic is not duplicated here:
it comes from ``domain.utils``, so the API layer and the application services resolve the same
identifiers.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
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
from i3x_server.application.services.model_query import _relationship_type_items as _model_relationship_type_items
from i3x_server.domain.utils import (
    I3X_NAMESPACE_URI,
    I3X_UNKNOWN_NAMESPACE_URI,
    canonical_namespace_uri,
    expanded_node_id,
    is_null_opcua_type_node_id,
    namespace_display_name,
    namespace_uri_for_node_id,
    namespace_uri_from_expanded_node_id,
    normalize_namespace_uri,
)
from i3x_server.model.revision import model_revision
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.objecttype_schema import build_object_type_schema
from i3x_server.schemas.state import BuildResult


def _is_opcua_node_id(value: str) -> bool:
    """Whether a string is an OPC UA node id (``nsu=`` or an identifier form, with ``ns=`` optional)."""
    if value.startswith("nsu="):
        return True
    return bool(re.match(r"^(?:ns=\d+;)?[isgb]=.+$", value, flags=re.IGNORECASE))


def _to_namespace(item: OpcUaNamespaceInfo) -> Namespace:
    """Namespace response model for an upstream namespace definition."""
    return Namespace(uri=item.uri, displayName=namespace_display_name(item))


def _to_element_id(name: str) -> str:
    """i3X element id for a type name (``PumpUnitType`` becomes ``pump-unit-type``)."""
    normalized = re.sub(r"Type$", "-type", name)
    split = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", normalized)
    lowered = split.replace("_", "-").lower()
    compact = re.sub(r"-+", "-", lowered).strip("-")
    return compact or "unknown-type"


def _to_urn_token(value: str) -> str:
    """Lowercase, hyphen-separated URN token; anything non-alphanumeric collapses to ``-``."""
    lowered = value.lower()
    token = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return token or "unknown"


def _server_uri_for_namespace_infos(namespace_infos: list[OpcUaNamespaceInfo]) -> str:
    """Server URI the namespaces declare, or ``unknown-server`` when none carries one."""
    for item in namespace_infos:
        if isinstance(item.server_uri, str) and item.server_uri.strip():
            return item.server_uri.strip()
    return "unknown-server"


def _namespace_version_for_uri(namespace_uri: str, namespace_infos: list[OpcUaNamespaceInfo]) -> str:
    """Version the server declares for a namespace URI, or ``unknown-version``."""
    normalized_uri = normalize_namespace_uri(namespace_uri)
    for item in namespace_infos:
        if normalize_namespace_uri(item.uri) == normalized_uri:
            if isinstance(item.namespace_version, str) and item.namespace_version.strip():
                return item.namespace_version.strip()
            break
    return "unknown-version"


def _server_uri_token(server_uri: str) -> str:
    """URN token for the server URI, with the ``urn:`` prefix removed."""
    normalized = re.sub(r"^urn:", "", server_uri.strip(), flags=re.IGNORECASE)
    return _to_urn_token(normalized)


def _object_type_element_id(
    item: OpcUaObjectTypeInfo,
    namespace_uri: str,
    server_uri: str,
    namespace_version: str,
) -> str:
    """Element id of a listed object type: server, namespace, version, browse name, node id.

    The shape is shared with :func:`_virtual_object_type_element_id` so served types look the
    same whether or not the server listed them.
    """
    return ":".join(
        [
            "urn",
            _server_uri_token(server_uri),
            "objecttype",
            _to_urn_token(namespace_uri),
            f"v-{_to_urn_token(namespace_version)}",
            _to_urn_token(item.browse_name),
            _to_urn_token(item.node_id.split(";", 1)[-1]),
        ]
    )


def _virtual_object_type_element_id(
    namespace_uri: str,
    display_name: str,
    source_type_id: str,
    server_uri: str,
    namespace_version: str,
) -> str:
    """Element id for a type the server did not list; the browse name slot holds the display name."""
    return ":".join(
        [
            "urn",
            _server_uri_token(server_uri),
            "objecttype",
            _to_urn_token(namespace_uri),
            f"v-{_to_urn_token(namespace_version)}",
            _to_urn_token(display_name),
            _to_urn_token(source_type_id.split(";", 1)[-1]),
        ]
    )


def _object_type_element_ids_by_node_id(
    object_types: list[OpcUaObjectTypeInfo],
    namespace_infos: list[OpcUaNamespaceInfo],
) -> dict[str, str]:
    """Element id per listed object type, keyed by its upstream node id."""
    return {
        item.node_id: _object_type_element_id(
            item,
            namespace_uri_for_node_id(item.node_id, namespace_infos),
            _server_uri_for_namespace_infos(namespace_infos),
            _namespace_version_for_uri(namespace_uri_for_node_id(item.node_id, namespace_infos), namespace_infos),
        )
        for item in object_types
    }


def _relationship_type_items(model: BuildResult | None = None) -> list[RelationshipType]:
    """Relationship types for the response, validated from the model's canonical items."""
    return [RelationshipType.model_validate(item) for item in _model_relationship_type_items(model)]


def _find_model_node(model: BuildResult, element_id: str) -> ModelNode | None:
    """Model node for an element id.

    The id is looked up directly, then through the name and type indexes, then by name, type, and
    source node id (case-insensitive last) - clients may address an element by any of those.
    """
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
    """Hierarchy parent of a node.

    Roots have no parent, and an asset or event source in the hierarchy plane is a top-level node
    even when it appears in the ordinary children map.
    """
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

    return _reverse_parent_index(model).get(node_id)


def _reverse_parent_index(model: BuildResult) -> dict[str, str]:
    """child id -> first parent listing it (hierarchy children first, then children_by_id).

    The model owns its cached index so discarded models are not retained by a global cache.
    """
    raw_hierarchy_children_by_id = getattr(model, "hierarchy_children_by_id", None)
    hierarchy_children_by_id = raw_hierarchy_children_by_id if isinstance(raw_hierarchy_children_by_id, dict) else {}
    # The content revision is part of the fingerprint: two rebuilds with the same number of parents
    # and children can list *different* parents (a child moves between parents), and a length-only
    # fingerprint would keep serving the stale index. The plane sizes additionally catch in-place
    # mutations of an already-revisioned model, which is not a supported way to change content.
    fingerprint = (model_revision(model), len(hierarchy_children_by_id), len(model.children_by_id))
    cached = model._reverse_parent_index_cache
    if cached is not None and cached[0] == fingerprint:
        return cached[1]

    index: dict[str, str] = {}
    for parent_id, child_ids in hierarchy_children_by_id.items():
        if not isinstance(parent_id, str) or not isinstance(child_ids, list):
            continue
        for child_id in child_ids:
            if isinstance(child_id, str):
                index.setdefault(child_id, parent_id)
    for parent_id, child_ids in model.children_by_id.items():
        for child_id in child_ids:
            if isinstance(child_id, str):
                index.setdefault(child_id, parent_id)

    model._reverse_parent_index_cache = (fingerprint, index)
    return index


def _hierarchy_children_for_node(model: BuildResult, node: ModelNode) -> list[str]:
    """Children in the hierarchy plane; the ordinary children map is the fallback."""
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
    """Children in the composition plane, i.e. the properties a node is composed of."""
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
    """Relationship targets for a node: the declared map when present, else the derived one."""
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
    """Reverse of ``HasChildren``: a property hangs off its owner as ``ComponentOf``."""
    if node.kind == "property":
        return RelationshipType(
            elementId="ComponentOf",
            displayName="ComponentOf",
            namespaceUri=I3X_NAMESPACE_URI,
            relationshipId="ComponentOf",
            reverseOf="HasComponent",
        )
    return RelationshipType(
        elementId="HasParent",
        displayName="HasParent",
        namespaceUri=I3X_NAMESPACE_URI,
        relationshipId="HasParent",
        reverseOf="HasChildren",
    )


def _relationship_type_for_name(name: str, node: ModelNode) -> RelationshipType:
    """Relationship type for a name in the i3X vocabulary; unknown names pass through as-is."""
    if name == "HasChildren":
        return RelationshipType(
            elementId="HasChildren",
            displayName="HasChildren",
            namespaceUri=I3X_NAMESPACE_URI,
            relationshipId="HasChildren",
            reverseOf="HasParent",
        )
    if name == "HasParent":
        return _relationship_type_to_parent(node)
    if name == "HasComponent":
        return RelationshipType(
            elementId="HasComponent",
            displayName="HasComponent",
            namespaceUri=I3X_NAMESPACE_URI,
            relationshipId="HasComponent",
            reverseOf="ComponentOf",
        )
    if name == "ComponentOf":
        return RelationshipType(
            elementId="ComponentOf",
            displayName="ComponentOf",
            namespaceUri=I3X_NAMESPACE_URI,
            relationshipId="ComponentOf",
            reverseOf="HasComponent",
        )
    return RelationshipType(
        elementId=name,
        displayName=name,
        namespaceUri=I3X_NAMESPACE_URI,
        relationshipId=name,
        reverseOf="",
    )


def _resolve_type_namespace_uri(
    type_element_id: str,
    source_type_id_expanded: str,
    namespace_infos: list[OpcUaNamespaceInfo],
) -> str | None:
    """Namespace URI of a type, read from the element id and refined by the declared namespaces."""
    type_namespace_uri = namespace_uri_from_expanded_node_id(type_element_id)
    if type_namespace_uri is None and _is_opcua_node_id(type_element_id):
        resolved_type_namespace_uri = namespace_uri_for_node_id(type_element_id, namespace_infos)
        if resolved_type_namespace_uri:
            type_namespace_uri = resolved_type_namespace_uri
    if type_namespace_uri is None:
        type_namespace_uri = namespace_uri_from_expanded_node_id(source_type_id_expanded)
    if type_namespace_uri is not None:
        type_namespace_uri = canonical_namespace_uri(type_namespace_uri, namespace_infos)
    return type_namespace_uri


def _build_object_instance_metadata(
    model: BuildResult,
    node: ModelNode,
    type_namespace_uri: str | None,
    source_type_id_expanded: str,
) -> ObjectInstanceMetadata | None:
    """Metadata block of an object instance: relationships, composition parent, description."""
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

    description = node.metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        description = f"Derived from model node {node.name}"

    return ObjectInstanceMetadata(
        typeNamespaceUri=type_namespace_uri,
        sourceTypeId=source_type_id_expanded,
        description=description,
        compositionParentId=composition_parent_id,
        relationships=relationships,
    )


def _unknown_type_element_id(namespace_infos: list[OpcUaNamespaceInfo]) -> str:
    """Placeholder type element id for a node whose source type cannot be resolved."""
    namespace_uri = namespace_infos[0].uri if namespace_infos else I3X_UNKNOWN_NAMESPACE_URI
    namespace_uri = canonical_namespace_uri(namespace_uri, namespace_infos) if namespace_infos else namespace_uri
    return _virtual_object_type_element_id(
        namespace_uri,
        "UnknownType",
        "nsu=http://opcfoundation.org/UA/;i=0",
        _server_uri_for_namespace_infos(namespace_infos),
        _namespace_version_for_uri(namespace_uri, namespace_infos),
    )


def _resolved_type_element_id_for_node(
    node: ModelNode,
    namespace_infos: list[OpcUaNamespaceInfo],
    object_type_element_ids_by_node_id: dict[str, str],
    object_type_element_ids_by_source_type: dict[str, str],
) -> str:
    """Type element id for a node, resolved through the listed-type indexes or the placeholder.

    Properties carry their DataType rather than a source type id, and a null DataType
    (``nsu=...;i=0``) is the "no type" marker, so it maps to the placeholder type.
    """
    if node.kind == "property":
        raw_type_element_id = node.type or "unknown-type"
        type_element_id = expanded_node_id(raw_type_element_id, namespace_infos)
        if is_null_opcua_type_node_id(type_element_id):
            return _unknown_type_element_id(namespace_infos)
        return object_type_element_ids_by_source_type.get(type_element_id.lower(), type_element_id)

    source_type_id = node.source_type_id
    if not source_type_id:
        return _unknown_type_element_id(namespace_infos)

    source_type_id_expanded = expanded_node_id(source_type_id, namespace_infos)
    if is_null_opcua_type_node_id(source_type_id_expanded):
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
    """Object instance response for a model node, with metadata only when asked for."""
    source_type_id = node.source_type_id or node.source_node_id
    source_type_id_expanded = expanded_node_id(source_type_id, namespace_infos)
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
    """Instances the model reports for a type node id, with metadata."""
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
    resolved_data_type_schemas: Mapping[str, Mapping[str, Any]] | None = None,
) -> ObjectTypeResponse:
    """Object type response: element id, source type, schema, and the instances of the type."""
    namespace_uri = namespace_uri_for_node_id(item.node_id, namespace_infos)
    element_id = _object_type_element_id(
        item,
        namespace_uri,
        _server_uri_for_namespace_infos(namespace_infos),
        _namespace_version_for_uri(namespace_uri, namespace_infos),
    )
    source_type_id = expanded_node_id(item.node_id, namespace_infos)
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
        schema=build_object_type_schema(
            item,
            object_types_by_node_id,
            element_ids_by_node_id,
            namespace_infos,
            resolved_data_type_schemas=resolved_data_type_schemas,
        ),
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
    """Objects related to a node, optionally narrowed to one relationship type."""
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
