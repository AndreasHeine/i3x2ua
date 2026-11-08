from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from contextlib import nullcontext as _nullcontext
from copy import deepcopy
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from fastapi import Request

from i3x_server.api.v1.common_helpers import (
    _raise_invalid_argument,
    _to_json_safe_value,
)
from i3x_server.api.v1.contracts import (
    GetObjectHistoryRequest,
    Namespace,
    ObjectInstanceMetadata,
    ObjectInstanceResponse,
    ObjectTypeResponse,
    QueryCapabilities,
    RelationshipType,
    ServerCapabilities,
    ServerInfo,
    StreamRequest,
    SubscribeCapabilities,
    UpdateCapabilities,
)
from i3x_server.api.v1.objecttype_helpers import (
    _datatype_object_type_from_source_type_id,
    _generic_object_type_from_source_type_id,
    _live_type_name_lookup_max_per_request,
    _opaque_datatype_object_type_from_source_type_id,
    _scalar_schema_for_standard_ua_datatype_node_id,
)
from i3x_server.application.ports.opcua import (
    OpcUaClientProtocol,
    OpcUaNamespaceInfo,
    OpcUaObjectTypeInfo,
)
from i3x_server.application.services.model_query import _relationship_type_items as _model_relationship_type_items
from i3x_server.config.settings import Settings, get_settings
from i3x_server.domain.utils import (
    canonical_namespace_uri as _canonical_namespace_uri,
)
from i3x_server.domain.utils import (
    expanded_node_id as _expanded_node_id,
)
from i3x_server.domain.utils import (
    is_null_opcua_type_node_id as _is_null_opcua_type_node_id,
)
from i3x_server.domain.utils import (
    namespace_uri_for_node_id as _namespace_uri_for_node_id,
)
from i3x_server.domain.utils import (
    namespace_uri_from_expanded_node_id as _namespace_uri_from_expanded_node_id,
)
from i3x_server.domain.utils import (
    normalize_namespace_uri as _normalize_namespace_uri,
)
from i3x_server.domain.utils import (
    server_name_from_openapi,
)
from i3x_server.errors import i3x_http_error
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.objecttype_schema import (
    build_object_type_schema,
    deduplicate_schema_fragments,
    remove_opcua_schema_fields,
)
from i3x_server.schemas.state import BuildResult
from i3x_server.version import get_server_version

logger = logging.getLogger(__name__)

__all__ = [
    "GetObjectHistoryRequest",
    "StreamRequest",
    "_canonical_namespace_uri",
    "_expanded_node_id",
    "_namespace_uri_for_node_id",
    "_namespace_uri_from_expanded_node_id",
    "_scalar_schema_for_standard_ua_datatype_node_id",
    "stream_subscription_v1",
    "_raise_invalid_argument",
    "_to_json_safe_value",
]


def _runtime_settings() -> Settings:
    return get_settings()


def _stream_debug_enabled() -> bool:
    return _runtime_settings().debug_subscription_stream


def _writes_enabled() -> bool:
    return _runtime_settings().enable_writes


def _include_mcp_opcua_metadata() -> bool:
    return _runtime_settings().mcp_include_opcua_metadata


@dataclass(slots=True)
class _ObjectTypeContext:
    namespace_infos: list[OpcUaNamespaceInfo]
    object_types: list[OpcUaObjectTypeInfo]
    element_ids_by_node_id: dict[str, str]
    items: list[ObjectTypeResponse]
    source_type_to_element_id: dict[str, str]


def _not_implemented(feature: str) -> None:
    raise i3x_http_error(
        501,
        "NotImplemented",
        f"{feature} is not implemented in this server",
        {"feature": feature},
    )


def _server_name_from_openapi(default_name: str = "The i3X API Gateway for OPC UA") -> str:
    return server_name_from_openapi(default_name)


def _supported_capabilities(history_update_supported: bool | None = None) -> ServerCapabilities:
    # `query.history` is honest: POST /objects/history is always implemented (spec :1294 makes the
    # endpoint a MUST) and answers a range without stored samples with GoodNoData, so the
    # declaration does not depend on whether the upstream server happens to store history.
    return ServerCapabilities(
        query=QueryCapabilities(history=True),
        update=UpdateCapabilities(
            current=_writes_enabled(),
            history=_writes_enabled() and history_update_supported is not False,
        ),
        subscribe=SubscribeCapabilities(stream=True),
    )


def _build_server_info() -> ServerInfo:
    return ServerInfo(
        specVersion="1.0",
        serverVersion=get_server_version(),
        serverName=_server_name_from_openapi(),
        capabilities=_supported_capabilities(),
    )


def _is_opcua_node_id(value: str) -> bool:
    if value.startswith("nsu="):
        return True
    return bool(re.match(r"^(?:ns=\d+;)?[isgb]=.+$", value, flags=re.IGNORECASE))


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


def _server_uri_for_namespace_infos(namespace_infos: list[OpcUaNamespaceInfo]) -> str:
    for item in namespace_infos:
        if isinstance(item.server_uri, str) and item.server_uri.strip():
            return item.server_uri.strip()
    return "unknown-server"


def _namespace_version_for_uri(namespace_uri: str, namespace_infos: list[OpcUaNamespaceInfo]) -> str:
    normalized_uri = _normalize_namespace_uri(namespace_uri)
    for item in namespace_infos:
        if _normalize_namespace_uri(item.uri) == normalized_uri:
            if isinstance(item.namespace_version, str) and item.namespace_version.strip():
                return item.namespace_version.strip()
            break
    return "unknown-version"


def _server_uri_token(server_uri: str) -> str:
    return _to_urn_token(re.sub(r"^urn:", "", server_uri.strip(), flags=re.IGNORECASE))


def _object_type_element_id(
    item: OpcUaObjectTypeInfo,
    namespace_uri: str,
    server_uri: str,
    namespace_version: str,
) -> str:
    # Keep element IDs stable, queryable, and unique across namespaces.
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
    # Keep synthetic structure IDs in the same namespace as regular objecttypes.
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


def _virtual_element_id(
    namespace_uri: str,
    display_name: str,
    source_type_id: str,
    namespace_infos: list[OpcUaNamespaceInfo],
) -> str:
    return _virtual_object_type_element_id(
        namespace_uri,
        display_name,
        source_type_id,
        _server_uri_for_namespace_infos(namespace_infos),
        _namespace_version_for_uri(namespace_uri, namespace_infos),
    )


def _to_object_type(
    item: OpcUaObjectTypeInfo,
    model: BuildResult,
    namespace_infos: list[OpcUaNamespaceInfo],
    object_types_by_node_id: dict[str, OpcUaObjectTypeInfo],
    element_ids_by_node_id: dict[str, str],
    element_ids_by_source_type: dict[str, str],
) -> ObjectTypeResponse:
    namespace_uri = _namespace_uri_for_node_id(item.node_id, namespace_infos)
    element_id = _object_type_element_id(
        item,
        namespace_uri,
        _server_uri_for_namespace_infos(namespace_infos),
        _namespace_version_for_uri(namespace_uri, namespace_infos),
    )
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


def _object_type_element_ids_by_node_id(
    object_types: list[OpcUaObjectTypeInfo],
    namespace_infos: list[OpcUaNamespaceInfo],
) -> dict[str, str]:
    return {
        item.node_id: _object_type_element_id(
            item,
            _namespace_uri_for_node_id(item.node_id, namespace_infos),
            _server_uri_for_namespace_infos(namespace_infos),
            _namespace_version_for_uri(_namespace_uri_for_node_id(item.node_id, namespace_infos), namespace_infos),
        )
        for item in object_types
    }


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


def _relationship_type_items(model: BuildResult | None = None) -> list[RelationshipType]:
    return [RelationshipType.model_validate(item) for item in _model_relationship_type_items(model)]


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
            # Node has no hierarchy parent; if it's an asset/event, it's a root
            node = model.nodes_by_id.get(node_id)
            if node and node.kind in {"asset", "eventSource"}:
                return None
            # For properties and other kinds, continue with fallback lookup

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
    if isinstance(raw_hierarchy_children_by_id, dict):
        if node.id in raw_hierarchy_children_by_id:
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
    if isinstance(raw_composition_children_by_id, dict):
        if node.id in raw_composition_children_by_id:
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


def _resolve_type_namespace_uri(
    type_element_id: str,
    source_type_id_expanded: str,
    namespace_infos: list[OpcUaNamespaceInfo],
) -> str | None:
    """Resolve the namespace URI for a type element, with fallback chain."""
    type_namespace_uri = _namespace_uri_from_expanded_node_id(type_element_id)
    if type_namespace_uri is None and _is_opcua_node_id(type_element_id):
        resolved_type_namespace_uri = _namespace_uri_for_node_id(type_element_id, namespace_infos)
        if resolved_type_namespace_uri:
            type_namespace_uri = resolved_type_namespace_uri
    if type_namespace_uri is None:
        type_namespace_uri = _namespace_uri_from_expanded_node_id(source_type_id_expanded)
    if type_namespace_uri is not None:
        type_namespace_uri = _canonical_namespace_uri(type_namespace_uri, namespace_infos)
    return type_namespace_uri


def _build_object_instance_metadata(
    model: BuildResult,
    node: ModelNode,
    type_namespace_uri: str | None,
    source_type_id_expanded: str,
) -> ObjectInstanceMetadata | None:
    """Construct object metadata from relationships, composition parent, and type info."""
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


def _unknown_type_element_id(
    namespace_infos: list[OpcUaNamespaceInfo],
) -> str:
    """Generate a URN element ID for nodes without a defined source type (e.g., actions).
    This ensures that all nodes have a typeElementId that resolves to a registered ObjectType.
    """
    namespace_uri = namespace_infos[0].uri if namespace_infos else "https://cesmii.org/i3x/unknown"
    namespace_uri = _canonical_namespace_uri(namespace_uri, namespace_infos) if namespace_infos else namespace_uri
    return _virtual_element_id(namespace_uri, "UnknownType", "nsu=http://opcfoundation.org/UA/;i=0", namespace_infos)


def _unknown_type_placeholder(
    element_id: str,
    namespace_infos: list[OpcUaNamespaceInfo],
) -> ObjectTypeResponse:
    namespace_uri = _namespace_uri_from_expanded_node_id(element_id)
    if namespace_uri is None:
        namespace_uri = _namespace_uri_for_node_id(element_id, namespace_infos)
    if not namespace_uri:
        namespace_uri = namespace_infos[0].uri if namespace_infos else "https://cesmii.org/i3x/unknown"
    namespace_uri = _canonical_namespace_uri(namespace_uri, namespace_infos) if namespace_infos else namespace_uri
    return ObjectTypeResponse(
        elementId=element_id,
        displayName="UnknownType",
        namespaceUri=namespace_uri,
        sourceTypeId=element_id,
        schema={
            "title": "UnknownType",
            "description": "Placeholder type generated for unresolved source type IDs",
        },
    )


def _object_type_alias_with_element_id(item: ObjectTypeResponse, element_id: str) -> ObjectTypeResponse:
    return ObjectTypeResponse(
        elementId=element_id,
        displayName=item.displayName,
        namespaceUri=item.namespaceUri,
        sourceTypeId=item.sourceTypeId,
        schema=deepcopy(item.schema_),
        related=deepcopy(item.related),
    )


def _is_builtin_ua_datatype_node_id(element_id: str) -> bool:
    match = re.match(r"^nsu=([^;]+);i=(\d+)$", element_id, flags=re.IGNORECASE)
    if match is None:
        return False

    namespace_uri = match.group(1).rstrip("/").lower()
    if namespace_uri != "http://opcfoundation.org/ua":
        return False

    identifier = int(match.group(2))
    # OPC UA Built-in DataTypes (NodeId namespace 0, i=1..25)
    return 1 <= identifier <= 25


def _collect_referenced_type_element_ids(
    model: BuildResult,
    namespace_infos: list[OpcUaNamespaceInfo],
    object_type_element_ids_by_node_id: dict[str, str],
) -> set[str]:
    referenced: set[str] = set()
    for node in model.nodes_by_id.values():
        if node.kind == "property":
            raw_type_element_id = node.type or "unknown-type"
            type_element_id = _expanded_node_id(raw_type_element_id, namespace_infos)
            # Filter out null type IDs (i=0) for properties; they should use UnknownType placeholder
            if _is_null_opcua_type_node_id(type_element_id):
                referenced.add(_unknown_type_element_id(namespace_infos))
                continue
        else:
            source_type_id = node.source_type_id
            if not source_type_id:
                # Nodes without source_type_id (e.g., actions) should resolve to UnknownType placeholder.
                referenced.add(_unknown_type_element_id(namespace_infos))
                continue
            source_type_id_expanded = _expanded_node_id(source_type_id, namespace_infos)
            type_element_id = object_type_element_ids_by_node_id.get(source_type_id, source_type_id_expanded)
        if _is_null_opcua_type_node_id(type_element_id):
            continue
        referenced.add(type_element_id)
    return referenced


def _collect_property_type_element_ids(
    model: BuildResult,
    namespace_infos: list[OpcUaNamespaceInfo],
) -> set[str]:
    property_type_ids: set[str] = set()
    for node in model.nodes_by_id.values():
        if node.kind != "property":
            continue
        raw_type = node.type or ""
        expanded = _expanded_node_id(raw_type, namespace_infos)
        if not expanded or _is_null_opcua_type_node_id(expanded):
            continue
        property_type_ids.add(expanded)
    return property_type_ids


async def _build_object_type_context(
    model: BuildResult,
    namespace_infos: list[OpcUaNamespaceInfo],
    opcua_client: OpcUaClientProtocol,
    object_types: list[OpcUaObjectTypeInfo],
) -> _ObjectTypeContext:
    started = perf_counter()
    object_types_by_node_id = {item.node_id: item for item in object_types}
    element_ids_by_node_id = _object_type_element_ids_by_node_id(object_types, namespace_infos)

    items: list[ObjectTypeResponse] = []
    # Process in chunks to avoid blocking the event loop for too long
    # (essential for OPC UA keep-alives during heavy processing)
    chunk_size = 50
    for i in range(0, len(object_types), chunk_size):
        chunk = object_types[i : i + chunk_size]
        items.extend(
            [
                _to_object_type(item, model, namespace_infos, object_types_by_node_id, element_ids_by_node_id, {})
                for item in chunk
            ]
        )
        await asyncio.sleep(0)

    items.extend(_synthetic_object_types_from_structure_defs(items, namespace_infos))

    referenced_type_element_ids = _collect_referenced_type_element_ids(
        model,
        namespace_infos,
        element_ids_by_node_id,
    )
    property_type_element_ids = _collect_property_type_element_ids(model, namespace_infos)
    known_ids = {item.elementId for item in items}
    by_source_type_id: dict[str, ObjectTypeResponse] = {item.sourceTypeId.lower(): item for item in items}
    browse_name_cache: dict[str, str | None] = {}
    lookup_budget = {"remaining": _live_type_name_lookup_max_per_request()}

    for unresolved_id in sorted(referenced_type_element_ids - known_ids):
        unresolved_key = unresolved_id.lower()
        source_match = by_source_type_id.get(unresolved_key)
        if source_match is not None:
            continue

        datatype_item = _datatype_object_type_from_source_type_id(unresolved_id, namespace_infos)
        if datatype_item is not None:
            items.append(datatype_item)
            by_source_type_id[unresolved_key] = datatype_item
            continue

        if unresolved_id in property_type_element_ids:
            opaque_datatype_item = _opaque_datatype_object_type_from_source_type_id(unresolved_id, namespace_infos)
            if opaque_datatype_item is not None:
                items.append(opaque_datatype_item)
                by_source_type_id[unresolved_key] = opaque_datatype_item
                continue

        generic_item = await _generic_object_type_from_source_type_id(
            unresolved_id,
            namespace_infos,
            opcua_client,
            browse_name_cache,
            lookup_budget,
        )
        if generic_item is not None:
            items.append(generic_item)
            by_source_type_id[unresolved_key] = generic_item
            continue

        unknown_item = _unknown_type_placeholder(unresolved_id, namespace_infos)
        items.append(unknown_item)
        by_source_type_id[unresolved_key] = unknown_item

    canonical_items = [
        item.model_copy(update={"namespaceUri": _canonical_namespace_uri(item.namespaceUri, namespace_infos)})
        for item in items
    ]
    if not _include_mcp_opcua_metadata():
        canonical_items = [
            item.model_copy(update={"schema_": remove_opcua_schema_fields(item.schema_)}) for item in canonical_items
        ]
    schemas = deduplicate_schema_fragments(item.schema_ for item in canonical_items)
    for item, schema in zip(canonical_items, schemas, strict=True):
        item.schema_ = schema
    source_type_to_element_id = {key: item.elementId for key, item in by_source_type_id.items()}
    logger.debug(
        "Object type context built model_nodes=%d object_types=%d items=%d duration_s=%.3f",
        len(model.nodes_by_id),
        len(object_types),
        len(canonical_items),
        perf_counter() - started,
    )
    return _ObjectTypeContext(
        namespace_infos=namespace_infos,
        object_types=object_types,
        element_ids_by_node_id=element_ids_by_node_id,
        items=canonical_items,
        source_type_to_element_id=source_type_to_element_id,
    )


async def _get_object_type_context(
    request: Request,
    model: BuildResult,
    opcua_client: OpcUaClientProtocol,
    namespace_infos: list[OpcUaNamespaceInfo] | None = None,
) -> _ObjectTypeContext:
    started = perf_counter()
    resolved_namespace_infos = (
        namespace_infos if namespace_infos is not None else await opcua_client.get_namespace_infos()
    )
    object_types = await opcua_client.get_object_types()

    # Shared lock to prevent concurrent heavy rebuilds from multiple clients.
    # Use stable content-derived tokens instead of object identity or the volatile
    # build timestamp so equivalent refreshed models share a valid cache entry.
    lock = getattr(request.app.state, "object_type_lock", None)
    async with lock if lock else _nullcontext():
        cache = getattr(request.app.state, "object_type_context_cache", None)
        model_token = (
            len(model.nodes_by_id),
            len(model.root_ids),
            len(model.property_to_node),
            len(model.action_to_method),
            tuple(sorted(model.nodes_by_id)),
            tuple(sorted(model.root_ids)),
        )
        namespace_token = tuple(
            (info.uri, info.display_name, info.namespace_version, info.server_uri) for info in resolved_namespace_infos
        )
        object_types_token = tuple(item.node_id for item in object_types)
        if isinstance(cache, dict):
            if (
                cache.get("model_token") == model_token
                and cache.get("namespace_token") == namespace_token
                and cache.get("object_types_token") == object_types_token
            ):
                cached_context = cache.get("context")
                if isinstance(cached_context, _ObjectTypeContext):
                    logger.debug(
                        "Object type context cache hit model_nodes=%d object_types=%d duration_s=%.3f",
                        len(model.nodes_by_id),
                        len(object_types),
                        perf_counter() - started,
                    )
                    return cached_context

        context = await _build_object_type_context(
            model=model,
            namespace_infos=resolved_namespace_infos,
            opcua_client=opcua_client,
            object_types=object_types,
        )
        request.app.state.object_type_context_cache = {
            "model_token": model_token,
            "namespace_token": namespace_token,
            "object_types_token": object_types_token,
            "context": context,
        }
        logger.info(
            "Object type context cache miss rebuilt model_nodes=%d object_types=%d items=%d duration_s=%.3f",
            len(model.nodes_by_id),
            len(object_types),
            len(context.items),
            perf_counter() - started,
        )
        return context


async def _get_object_endpoint_context(
    request: Request,
    model: BuildResult,
    opcua_client: OpcUaClientProtocol,
) -> tuple[list[OpcUaNamespaceInfo], dict[str, str], dict[str, str]]:
    try:
        namespace_infos = await opcua_client.get_namespace_infos()
    except Exception as exc:
        raise i3x_http_error(
            502,
            "OpcUaNamespaceError",
            "Failed to read OPC UA namespaces",
            {"cause": str(exc)},
        ) from exc

    object_type_element_ids_by_node_id: dict[str, str] = {}
    object_type_element_ids_by_source_type: dict[str, str] = {}
    try:
        context = await _get_object_type_context(
            request,
            model,
            opcua_client,
            namespace_infos=namespace_infos,
        )
        object_type_element_ids_by_node_id = context.element_ids_by_node_id
        object_type_element_ids_by_source_type = context.source_type_to_element_id
    except Exception:
        object_type_element_ids_by_node_id = {}
        object_type_element_ids_by_source_type = {}

    return namespace_infos, object_type_element_ids_by_node_id, object_type_element_ids_by_source_type


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
        # Nodes without source_type_id (e.g., actions, variables) should map to UnknownType placeholder.
        return _unknown_type_element_id(namespace_infos)

    source_type_id_expanded = _expanded_node_id(source_type_id, namespace_infos)
    if _is_null_opcua_type_node_id(source_type_id_expanded):
        return _unknown_type_element_id(namespace_infos)
    resolved = object_type_element_ids_by_node_id.get(source_type_id)
    if resolved is not None:
        return resolved
    return object_type_element_ids_by_source_type.get(source_type_id_expanded.lower(), source_type_id_expanded)


def _iter_local_defs_refs(value: Any) -> list[str]:
    refs: list[str] = []

    if isinstance(value, Mapping):
        ref_value = value.get("$ref")
        if isinstance(ref_value, str) and ref_value.startswith("#/$defs/"):
            refs.append(ref_value.split("#/$defs/", 1)[1])

        for nested in value.values():
            refs.extend(_iter_local_defs_refs(nested))
        return refs

    if isinstance(value, list):
        for nested in value:
            refs.extend(_iter_local_defs_refs(nested))

    return refs


def _collect_transitive_defs(schema: Mapping[str, Any], defs: Mapping[str, Any]) -> dict[str, Any]:
    collected: dict[str, Any] = {}
    pending = _iter_local_defs_refs(schema)
    seen: set[str] = set()

    while pending:
        def_name = pending.pop()
        if def_name in seen:
            continue
        seen.add(def_name)

        referenced = defs.get(def_name)
        if not isinstance(referenced, Mapping):
            continue

        referenced_copy = deepcopy(dict(referenced))
        collected[def_name] = referenced_copy
        pending.extend(_iter_local_defs_refs(referenced_copy))

    return collected


def _synthetic_object_types_from_structure_defs(
    listed_object_types: list[ObjectTypeResponse],
    namespace_infos: list[OpcUaNamespaceInfo],
) -> list[ObjectTypeResponse]:
    synthetic_by_source_type_id: dict[str, ObjectTypeResponse] = {}

    for item in listed_object_types:
        defs = item.schema_.get("$defs") if isinstance(item.schema_, Mapping) else None
        if not isinstance(defs, Mapping):
            continue

        for raw_def in defs.values():
            if not isinstance(raw_def, Mapping):
                continue

            source_hint = raw_def.get("x-opcua-structureDataType") or raw_def.get("x-opcua-structureTypeId")
            if not isinstance(source_hint, str) or not source_hint:
                continue

            source_type_id = _expanded_node_id(source_hint, namespace_infos)
            source_key = source_type_id.lower()
            if source_key in synthetic_by_source_type_id:
                continue

            namespace_uri = _namespace_uri_from_expanded_node_id(source_type_id)
            if namespace_uri is None:
                namespace_uri = _namespace_uri_for_node_id(source_type_id, namespace_infos)
            if not namespace_uri:
                namespace_uri = item.namespaceUri
            namespace_uri = _canonical_namespace_uri(namespace_uri, namespace_infos)

            display_name_raw = raw_def.get("title")
            display_name = (
                display_name_raw if isinstance(display_name_raw, str) and display_name_raw else "StructureType"
            )
            schema = deepcopy(dict(raw_def))
            required_defs = _collect_transitive_defs(schema, defs)
            if required_defs:
                schema["$defs"] = required_defs
            schema.setdefault("x-opcua-nodeId", source_type_id)
            schema.setdefault("x-opcua-displayName", display_name)
            if not _include_mcp_opcua_metadata():
                schema = remove_opcua_schema_fields(schema)

            synthetic_by_source_type_id[source_key] = ObjectTypeResponse(
                elementId=_virtual_element_id(namespace_uri, display_name, source_type_id, namespace_infos),
                displayName=display_name,
                namespaceUri=namespace_uri,
                sourceTypeId=source_type_id,
                schema=schema,
            )

    return list(synthetic_by_source_type_id.values())


def _namespace_index_from_node_id(node_id: str) -> int | None:
    match = re.match(r"^ns=(\d+);", node_id)
    if match is None:
        # OPC UA defaults to namespace index 0 when the ns prefix is omitted.
        if re.match(r"^[isgb]=", node_id):
            return 0
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


async def stream_subscription_v1(
    body: StreamRequest,
    opcua_client: OpcUaClientProtocol,
    subscription_app_service: Any,
) -> Any:
    """Compatibility shim for tests importing stream handler from monolithic module."""
    from i3x_server.api.v1.subscription_routes import stream_subscription_v1 as _stream_subscription_v1

    return await _stream_subscription_v1(
        body=body,
        opcua_client=opcua_client,
        subscription_app_service=subscription_app_service,
    )
