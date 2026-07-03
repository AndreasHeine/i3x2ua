from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from contextlib import nullcontext as _nullcontext
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from fastapi import APIRouter, Request
from typing_extensions import Never

from i3x_server.api.v1.common_helpers import (
    _format_utc_timestamp,
    _good_no_data_vqt,
    _now_iso,
    _raise_invalid_argument,
    _to_json_safe_value,
)
from i3x_server.api.v1.contracts import (
    VQT,
    GetObjectHistoryRequest,
    HistoricalValueResult,
    ModelBuildMetrics,
    ModelContextMetrics,
    ModelCoverageMetrics,
    ModelMetricsResponse,
    ModelQualityMetrics,
    ModelRelationshipMetrics,
    ModelVolumeMetrics,
    Namespace,
    ObjectInstanceMetadata,
    ObjectInstanceResponse,
    ObjectTypeResponse,
    QueryCapabilities,
    RelatedObjectResult,
    RelationshipType,
    ServerCapabilities,
    ServerInfo,
    StreamRequest,
    SubscribeCapabilities,
    UpdateCapabilities,
)
from i3x_server.api.v1.objecttype_helpers import (
    _datatype_object_type_from_source_type_id,
    _opaque_datatype_object_type_from_source_type_id,
    _scalar_schema_for_standard_ua_datatype_node_id,
    _standard_ua_type_name,
)
from i3x_server.application.ports.opcua import (
    OpcUaClientProtocol,
    OpcUaNamespaceInfo,
    OpcUaObjectTypeInfo,
)
from i3x_server.config.settings import Settings, get_settings
from i3x_server.domain.utils import server_name_from_openapi
from i3x_server.errors import i3x_http_error
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.objecttype_schema import build_object_type_schema
from i3x_server.schemas.state import BuildResult
from i3x_server.version import get_server_version

router = APIRouter(prefix="/v1", tags=["v1"])
logger = logging.getLogger(__name__)

__all__ = [
    "GetObjectHistoryRequest",
    "StreamRequest",
    "_scalar_schema_for_standard_ua_datatype_node_id",
    "stream_subscription_v1",
    "_expanded_node_id",
    "_raise_invalid_argument",
    "_to_json_safe_value",
]


def _runtime_settings() -> Settings:
    return get_settings()


_ENABLE_LIVE_TYPE_NAME_LOOKUP: bool = _runtime_settings().enable_type_browsename_lookup
_LIVE_TYPE_NAME_LOOKUP_TIMEOUT_S: float = _runtime_settings().type_browsename_lookup_timeout_s
_LIVE_TYPE_NAME_LOOKUP_MAX_PER_REQUEST: int = _runtime_settings().type_browsename_lookup_max


def _live_type_name_lookup_enabled() -> bool:
    return _ENABLE_LIVE_TYPE_NAME_LOOKUP


def _live_type_name_lookup_timeout_seconds() -> float:
    return _LIVE_TYPE_NAME_LOOKUP_TIMEOUT_S


def _live_type_name_lookup_max_per_request() -> int:
    return _LIVE_TYPE_NAME_LOOKUP_MAX_PER_REQUEST


def _stream_debug_enabled() -> bool:
    return _runtime_settings().debug_subscription_stream


def _writes_enabled() -> bool:
    return _runtime_settings().enable_writes


def _raise_subscription_not_found(subscription_id: str) -> Never:
    raise i3x_http_error(
        404,
        "SubscriptionNotFound",
        f"Subscription '{subscription_id}' not found",
    )


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


def _supported_capabilities() -> ServerCapabilities:
    return ServerCapabilities(
        query=QueryCapabilities(history=True),
        update=UpdateCapabilities(current=_writes_enabled(), history=_writes_enabled()),
        subscribe=SubscribeCapabilities(stream=True),
    )


def _is_valid_write_type(value: Any, variant_type: str | None) -> bool:
    if variant_type is None:
        return True
    normalized = variant_type.strip().lower()
    if normalized in {"boolean"}:
        return isinstance(value, bool)
    if normalized in {"sbyte", "byte", "int16", "uint16", "int32", "uint32", "int64", "uint64"}:
        return isinstance(value, int) and not isinstance(value, bool)
    if normalized in {"float", "double"}:
        return (isinstance(value, int) and not isinstance(value, bool)) or isinstance(value, float)
    if normalized in {"string", "localizedtext", "qualifiedname"}:
        return isinstance(value, str)
    if normalized in {"bytestring"}:
        return isinstance(value, (bytes, bytearray, memoryview))
    if normalized in {"datetime"}:
        return isinstance(value, (str, datetime))
    if normalized in {"guid"}:
        return isinstance(value, str)
    return True


def _classify_write_error(exc: Exception) -> tuple[int, str]:
    text = str(exc).lower()
    if "baduseraccessdenied" in text or "access denied" in text or "permission" in text:
        return 403, "unauthorized_by_opcua_server"
    if "badnotwritable" in text or "not writable" in text:
        return 403, "target_not_writable"
    if "badtype" in text or "mismatch" in text or "outofrange" in text:
        return 400, "bad_type_or_range"
    reconnect_markers = (
        "connection is closed",
        "connection is not open",
        "connection reset",
        "broken pipe",
        "socket",
        "transport closed",
        "timed out",
        "timeout",
        "badsessionclosed",
        "badsessionidinvalid",
        "badsecurechannelclosed",
    )
    if any(marker in text for marker in reconnect_markers):
        return 502, "session_or_transport_failure"
    return 502, "session_or_transport_failure"


def _raise_write_error(status_code: int, error_class: str) -> Never:
    raise i3x_http_error(status_code, "WriteError", error_class)


def _normalize_write_payload(payload: Any) -> Any:
    if isinstance(payload, Mapping):
        keys = set(payload.keys())
        if "value" in keys and keys <= {"value", "quality", "timestamp"}:
            return payload["value"]
    return payload


def _value_preview_for_log(value: Any, limit: int = 160) -> str:
    try:
        text = json.dumps(_to_json_safe_value(value), ensure_ascii=True, default=str)
    except Exception:
        text = str(value)
    return text if len(text) <= limit else f"{text[:limit]}..."


def _json_equivalent(left: Any, right: Any) -> bool:
    safe_left = _to_json_safe_value(left)
    safe_right = _to_json_safe_value(right)
    return json.dumps(safe_left, sort_keys=True) == json.dumps(safe_right, sort_keys=True)


async def _is_noop_write(
    *,
    opcua_client: OpcUaClientProtocol,
    target_node_id: str,
    requested_value: Any,
) -> bool:
    try:
        current_data_values = await opcua_client.read_data_values([target_node_id])
    except Exception:
        return False
    if not current_data_values:
        return False
    current_vqt = _vqt_from_data_value(current_data_values[0])
    return _json_equivalent(current_vqt.value, requested_value)


async def _write_object_value_by_element_id(
    *,
    model: BuildResult,
    opcua_client: OpcUaClientProtocol,
    element_id: str,
    payload_value: Any,
) -> tuple[bool, int, str, dict[str, Any]]:
    node = _find_model_node(model, element_id)
    write_value = _normalize_write_payload(payload_value)
    diagnostics: dict[str, Any] = {
        "requestedValueType": type(write_value).__name__,
        "requestedValuePreview": _value_preview_for_log(write_value),
        "resolvedVariantType": None,
    }
    if node is None:
        return False, 404, f"Element not found: {element_id}", diagnostics
    if node.kind != "property":
        return False, 400, "bad_type_or_range", diagnostics

    target_node_id = node.source_node_id

    try:
        writable, user_writable = await opcua_client.read_write_access(target_node_id)
    except Exception as exc:
        status_code, error_class = _classify_write_error(exc)
        diagnostics["exception"] = str(exc)
        return False, status_code, error_class, diagnostics

    if not writable or not user_writable:
        if await _is_noop_write(
            opcua_client=opcua_client,
            target_node_id=target_node_id,
            requested_value=write_value,
        ):
            return True, 200, "ok", diagnostics
        return False, 403, "target_not_writable", diagnostics

    try:
        variant_type = await opcua_client.read_variant_type(target_node_id)
        diagnostics["resolvedVariantType"] = variant_type
    except Exception as exc:
        status_code, error_class = _classify_write_error(exc)
        diagnostics["exception"] = str(exc)
        return False, status_code, error_class, diagnostics

    if not _is_valid_write_type(write_value, variant_type):
        return False, 400, "bad_type_or_range", diagnostics

    try:
        await opcua_client.write_value(target_node_id, write_value, variant_type=variant_type)
    except Exception as exc:
        status_code, error_class = _classify_write_error(exc)
        diagnostics["exception"] = str(exc)
        return False, status_code, error_class, diagnostics

    return True, 200, "ok", diagnostics


def _build_server_info() -> ServerInfo:
    return ServerInfo(
        specVersion="1.0",
        serverVersion=get_server_version(),
        serverName=_server_name_from_openapi(),
        capabilities=_supported_capabilities(),
    )


def _namespace_infos_by_uri(namespace_infos: list[OpcUaNamespaceInfo]) -> dict[str, OpcUaNamespaceInfo]:
    return {item.uri: item for item in namespace_infos}


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

    # Namespace index 0 is the OPC UA standard namespace.
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
    # Keep element IDs stable, queryable, and unique across namespaces.
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
    # Keep synthetic structure IDs in the same namespace as regular objecttypes.
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


def _object_type_element_ids_by_node_id(
    object_types: list[OpcUaObjectTypeInfo],
    namespace_infos: list[OpcUaNamespaceInfo],
) -> dict[str, str]:
    return {
        item.node_id: _object_type_element_id(item, _namespace_uri_for_node_id(item.node_id, namespace_infos))
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


_I3X_NAMESPACE = "https://cesmii.org/i3x"
_OPCUA_NAMESPACE = "https://opcfoundation.org/UA/"


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


def _relationship_type_for_name(name: str, node: ModelNode) -> RelationshipType:
    if name == "HasChildren":
        return RelationshipType(
            elementId="HasChildren",
            displayName="HasChildren",
            namespaceUri="https://cesmii.org/i3x",
            relationshipId="HasChildren",
            reverseOf="HasParent",
        )
    if name == "HasParent":
        return _relationship_type_to_parent(node)
    if name == "HasComponent":
        return RelationshipType(
            elementId="HasComponent",
            displayName="HasComponent",
            namespaceUri="https://cesmii.org/i3x",
            relationshipId="HasComponent",
            reverseOf="ComponentOf",
        )
    if name == "ComponentOf":
        return RelationshipType(
            elementId="ComponentOf",
            displayName="ComponentOf",
            namespaceUri="https://cesmii.org/i3x",
            relationshipId="ComponentOf",
            reverseOf="HasComponent",
        )
    return RelationshipType(
        elementId=name,
        displayName=name,
        namespaceUri="https://cesmii.org/i3x",
        relationshipId=name,
        reverseOf="",
    )


def _resolve_type_namespace_uri(
    type_element_id: str,
    source_type_id_expanded: str,
    namespace_infos: list[OpcUaNamespaceInfo],
) -> str | None:
    """Resolve the namespace URI for a type element, with fallback chain."""
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

    return ObjectInstanceMetadata(
        typeNamespaceUri=type_namespace_uri,
        sourceTypeId=source_type_id_expanded,
        description=f"Derived from model node {node.name}",
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


def _relationship_type_for_child(child: ModelNode) -> RelationshipType:
    if child.kind == "property":
        return RelationshipType(
            elementId="HasComponent",
            displayName="HasComponent",
            namespaceUri="https://cesmii.org/i3x",
            relationshipId="HasComponent",
            reverseOf="ComponentOf",
        )
    return RelationshipType(
        elementId="HasChildren",
        displayName="HasChildren",
        namespaceUri="https://cesmii.org/i3x",
        relationshipId="HasChildren",
        reverseOf="HasParent",
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
    """Build related object results for a given node with optional relationship filtering."""
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


def _parse_iso_datetime(value: str, field_name: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        _raise_invalid_argument(
            field_name,
            value,
            f"Invalid ISO 8601 timestamp for '{field_name}'",
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_history_time_range(body: GetObjectHistoryRequest) -> tuple[datetime | None, datetime | None]:
    start_time = _parse_iso_datetime(body.startTime, "startTime")
    end_time = _parse_iso_datetime(body.endTime, "endTime")
    if start_time > end_time:
        _raise_invalid_argument(
            "startTime/endTime",
            None,
            "startTime must be less than or equal to endTime",
        )
    return start_time, end_time


def _collect_history_lookup_and_node_ids(
    model: BuildResult,
    resolved_nodes: list[tuple[str, ModelNode | None]],
    max_depth: int,
) -> tuple[list[tuple[str, ModelNode, list[ModelNode], list[ModelNode]]], list[str]]:
    """Collect history source nodes and node IDs for OPC UA read."""
    lookup: list[tuple[str, ModelNode, list[ModelNode], list[ModelNode]]] = []
    node_ids: list[str] = []
    for element_id, node in resolved_nodes:
        if node is None:
            continue
        root_source_nodes = [node] if node.kind == "property" else []
        component_nodes = _collect_value_component_nodes(model, node, max_depth)
        lookup.append((element_id, node, root_source_nodes, component_nodes))
        for source_node in root_source_nodes:
            node_ids.append(source_node.source_node_id)
        for source_node in component_nodes:
            node_ids.append(source_node.source_node_id)
    return lookup, node_ids


def _build_historical_value_result(
    model: BuildResult,
    node: ModelNode,
    root_source_nodes: list[ModelNode],
    component_nodes: list[ModelNode],
    values_by_node_id: dict[str, list[Any]],
) -> HistoricalValueResult:
    """Build HistoricalValueResult from collected source nodes and values."""
    values: list[VQT] = []
    for source_node in root_source_nodes:
        raw_values = values_by_node_id.get(source_node.source_node_id, [])
        values.extend(_to_vqt_from_history_value(item) for item in raw_values)
    values.sort(key=lambda item: item.timestamp)

    components: dict[str, HistoricalValueResult] = {}
    for component_node in component_nodes:
        raw_values = values_by_node_id.get(component_node.source_node_id, [])
        component_values = [_to_vqt_from_history_value(item) for item in raw_values]
        component_values.sort(key=lambda item: item.timestamp)
        components[component_node.id] = HistoricalValueResult(
            isComposition=bool(_composition_children_for_node(model, component_node)),
            values=component_values,
        )

    return HistoricalValueResult(
        isComposition=bool(_composition_children_for_node(model, node)),
        values=values,
        components=components or None,
    )


def _vqt_from_any(value: Any) -> VQT:
    if value is None:
        return _good_no_data_vqt()
    return VQT(value=_to_json_safe_value(value), quality="Good", timestamp=_now_iso())


def _vqt_from_data_value(data_value: Any) -> VQT:
    variant = getattr(data_value, "Value", None)
    raw_value = getattr(variant, "Value", variant)
    status_code = getattr(data_value, "StatusCode", None)
    quality = _normalize_quality(status_code)
    source_timestamp = getattr(data_value, "SourceTimestamp", None)
    server_timestamp = getattr(data_value, "ServerTimestamp", None)
    timestamp_dt = source_timestamp or server_timestamp
    timestamp = _normalize_timestamp(timestamp_dt)
    safe_value = _to_json_safe_value(raw_value)
    if safe_value is None and quality not in ("Bad", "GoodNoData"):
        quality = "GoodNoData"
    return VQT(value=safe_value, quality=quality, timestamp=timestamp)


def _collect_value_component_nodes(model: BuildResult, root: ModelNode, max_depth: int) -> list[ModelNode]:
    if max_depth == 1:
        return []

    components: list[ModelNode] = []
    queue: list[tuple[str, int]] = [(root.id, 0)]
    visited: set[str] = set()

    while queue:
        node_id, depth = queue.pop(0)
        if node_id in visited:
            continue
        visited.add(node_id)

        if max_depth > 0 and depth >= max_depth:
            continue

        current_node = model.nodes_by_id.get(node_id)
        if current_node is None:
            continue

        for child_id in _composition_children_for_node(model, current_node):
            child = model.nodes_by_id.get(child_id)
            if child is None:
                continue
            if child.kind == "property":
                components.append(child)
            else:
                queue.append((child.id, depth + 1))

    return components


def _collect_history_source_nodes(model: BuildResult, root: ModelNode, max_depth: int) -> list[ModelNode]:
    if root.kind == "property":
        return [root]

    results: list[ModelNode] = []
    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(root.id, 1)]

    while queue:
        current_id, depth = queue.pop(0)
        if current_id in visited:
            continue
        visited.add(current_id)

        current = model.nodes_by_id.get(current_id)
        if current is None:
            continue
        if current.kind == "property":
            results.append(current)

        if max_depth != 0 and depth >= max_depth:
            continue

        current_node = model.nodes_by_id.get(current_id)
        if current_node is None:
            continue

        for child_id in _composition_children_for_node(model, current_node):
            queue.append((child_id, depth + 1))

    return results


def _normalize_quality(status_code: Any) -> str:
    if status_code is None:
        return "Good"
    is_uncertain = getattr(status_code, "is_uncertain", None)
    if callable(is_uncertain):
        try:
            if bool(is_uncertain()):
                return "Uncertain"
        except Exception:
            pass
    name = getattr(status_code, "name", "")
    label = str(name) if name else ""
    if "uncertain" in label.lower():
        return "Uncertain"
    is_good = getattr(status_code, "is_good", None)
    if callable(is_good):
        try:
            return "Good" if bool(is_good()) else "Bad"
        except Exception:
            pass
    if "good" in label.lower():
        return "Good"
    if label:
        return "Bad"
    return "Bad"


def _normalize_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return _format_utc_timestamp(value)
    return _format_utc_timestamp(datetime.now(timezone.utc))


def _relationship_type_to_parent(node: ModelNode) -> RelationshipType:
    if node.kind == "property":
        return RelationshipType(
            elementId="ComponentOf",
            displayName="ComponentOf",
            namespaceUri="https://cesmii.org/i3x",
            relationshipId="ComponentOf",
            reverseOf="HasComponent",
        )
    return RelationshipType(
        elementId="HasParent",
        displayName="HasParent",
        namespaceUri="https://cesmii.org/i3x",
        relationshipId="HasParent",
        reverseOf="HasChildren",
    )


def _unknown_type_element_id(
    namespace_infos: list[OpcUaNamespaceInfo],
) -> str:
    """Generate a URN element ID for nodes without a defined source type (e.g., actions).
    This ensures that all nodes have a typeElementId that resolves to a registered ObjectType.
    """
    namespace_uri = namespace_infos[0].uri if namespace_infos else "https://cesmii.org/i3x/unknown"
    namespace_uri = _canonical_namespace_uri(namespace_uri, namespace_infos) if namespace_infos else namespace_uri
    return _virtual_object_type_element_id(namespace_uri, "UnknownType", "nsu=http://opcfoundation.org/UA/;i=0")


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


def _is_standard_ua_namespace_node_id(element_id: str) -> bool:
    expanded_match = re.match(r"^nsu=([^;]+);", element_id, flags=re.IGNORECASE)
    if expanded_match is not None:
        namespace_uri = expanded_match.group(1).rstrip("/").lower()
        return namespace_uri == "http://opcfoundation.org/ua"

    indexed_match = re.match(r"^ns=(\d+);", element_id, flags=re.IGNORECASE)
    if indexed_match is None:
        return False
    return int(indexed_match.group(1)) == 0


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
            items.append(_object_type_alias_with_element_id(source_match, unresolved_id))
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

    # Shared lock to prevent concurrent heavy rebuilds from multiple clients
    lock = getattr(request.app.state, "object_type_lock", None)
    async with lock if lock else _nullcontext():
        cache = getattr(request.app.state, "object_type_context_cache", None)
        model_token = id(model)
        namespace_token = id(resolved_namespace_infos)
        object_types_token = id(object_types)
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

            synthetic_by_source_type_id[source_key] = ObjectTypeResponse(
                elementId=_virtual_object_type_element_id(namespace_uri, display_name, source_type_id),
                displayName=display_name,
                namespaceUri=namespace_uri,
                sourceTypeId=source_type_id,
                schema=schema,
            )

    return list(synthetic_by_source_type_id.values())


async def _generic_object_type_from_source_type_id(
    source_type_id: str,
    namespace_infos: list[OpcUaNamespaceInfo],
    opcua_client: OpcUaClientProtocol,
    browse_name_cache: dict[str, str | None],
    lookup_budget: dict[str, int],
) -> ObjectTypeResponse | None:
    expanded_match = re.match(r"^nsu=([^;]+);([isgb])=(.+)$", source_type_id, flags=re.IGNORECASE)
    if expanded_match is None:
        return None

    namespace_uri = expanded_match.group(1)
    identifier_kind = expanded_match.group(2).lower()
    identifier_value = expanded_match.group(3)

    display_name: str | None = None
    if identifier_kind == "i":
        standard_name = _standard_ua_type_name(source_type_id)
        if standard_name and "_" not in standard_name:
            display_name = standard_name
    elif identifier_kind == "s":
        token = identifier_value.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
        token = token.strip()
        if token:
            display_name = token

    if not display_name:
        display_name = f"InferredType_{identifier_kind}_{identifier_value}"

    if (
        _live_type_name_lookup_enabled()
        and display_name.startswith("InferredType_")
        and identifier_kind == "i"
        and lookup_budget.get("remaining", 0) > 0
    ):
        lookup_node_id = source_type_id
        if _is_standard_ua_namespace_node_id(source_type_id):
            lookup_node_id = f"ns=0;i={identifier_value}"
        resolved_name = browse_name_cache.get(lookup_node_id)
        if resolved_name is None:
            browse_name_reader = getattr(opcua_client, "read_browse_name", None)
            if callable(browse_name_reader):
                lookup_budget["remaining"] = max(0, lookup_budget.get("remaining", 0) - 1)
                try:
                    resolved_name = await asyncio.wait_for(
                        browse_name_reader(lookup_node_id),
                        timeout=_live_type_name_lookup_timeout_seconds(),
                    )
                except Exception:
                    resolved_name = None
            browse_name_cache[lookup_node_id] = resolved_name
        if isinstance(resolved_name, str) and resolved_name.strip():
            display_name = resolved_name.strip()

    canonical_namespace_uri = _canonical_namespace_uri(namespace_uri, namespace_infos)
    schema_payload = {
        "type": "object",
        "title": display_name,
        "description": "Generic placeholder schema inferred from source type ID",
        "x-opcua-nodeId": source_type_id,
        "x-opcua-displayName": display_name,
    }
    return ObjectTypeResponse(
        elementId=_virtual_object_type_element_id(canonical_namespace_uri, display_name, source_type_id),
        displayName=display_name,
        namespaceUri=canonical_namespace_uri,
        sourceTypeId=source_type_id,
        schema=schema_payload,
    )


def _require_client_id(client_id: str | None, endpoint: str) -> str:
    normalized = (client_id or "").strip()
    if normalized:
        return normalized
    _raise_invalid_argument(
        "clientId",
        None,
        f"'{endpoint}' requires a non-empty clientId",
    )


def _to_vqt_from_history_value(data_value: Any) -> VQT:
    variant = getattr(data_value, "Value", None)
    value = getattr(variant, "Value", variant)
    timestamp = (
        getattr(data_value, "SourceTimestamp", None)
        or getattr(data_value, "ServerTimestamp", None)
        or getattr(data_value, "timestamp", None)
    )
    quality = _normalize_quality(getattr(data_value, "StatusCode", None) or getattr(data_value, "status", None))
    return VQT(value=_to_json_safe_value(value), quality=quality, timestamp=_normalize_timestamp(timestamp))


def _increment_counter(counter: dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def _build_model_metrics(model: BuildResult) -> ModelMetricsResponse:
    kind_counts: dict[str, int] = {}
    for node in model.nodes_by_id.values():
        _increment_counter(kind_counts, str(node.kind))

    confidence_counts: dict[str, int] = {}
    semantic_role_counts: dict[str, int] = {}
    for node_id, node in model.nodes_by_id.items():
        confidence = model.mapping_confidence_by_id.get(node_id, node.mapping_confidence)
        semantic_role = model.semantic_role_by_id.get(node_id, node.semantic_role)
        _increment_counter(confidence_counts, str(confidence))
        _increment_counter(semantic_role_counts, str(semantic_role))

    namespace_counts: dict[str, int] = {}
    nodes_without_namespace = 0
    for node_id in model.nodes_by_id:
        namespace_uri = model.namespace_uri_by_id.get(node_id)
        if isinstance(namespace_uri, str) and namespace_uri.strip():
            _increment_counter(namespace_counts, namespace_uri)
        else:
            nodes_without_namespace += 1

    applied_profile_counts: dict[str, int] = {}
    nodes_without_profiles = 0
    for node_id in model.nodes_by_id:
        profile_ids = model.applied_profile_ids_by_id.get(node_id, [])
        normalized = [profile_id for profile_id in profile_ids if isinstance(profile_id, str) and profile_id]
        if not normalized:
            nodes_without_profiles += 1
            continue
        for profile_id in set(normalized):
            _increment_counter(applied_profile_counts, profile_id)

    relationship_name_counts: dict[str, int] = {}
    for per_source in model.relationships_by_id.values():
        for relationship_name, targets in per_source.items():
            if not isinstance(relationship_name, str) or not relationship_name:
                continue
            relationship_name_counts[relationship_name] = relationship_name_counts.get(relationship_name, 0) + len(
                targets
            )

    hierarchy_edges = sum(len(child_ids) for child_ids in model.hierarchy_children_by_id.values())
    composition_edges = sum(len(child_ids) for child_ids in model.composition_children_by_id.values())
    graph_edges = sum(len(relations) for relations in model.graph_related_by_id.values())
    typed_instances = sum(len(instance_ids) for instance_ids in model.instances_by_type_id.values())

    return ModelMetricsResponse(
        build=ModelBuildMetrics(
            browseDurationS=model.browse_duration_s,
            mapDurationS=model.map_duration_s,
            totalDurationS=model.total_duration_s,
            buildCompletedAtUtc=model.build_completed_at_utc,
        ),
        volume=ModelVolumeMetrics(
            totalNodes=len(model.nodes_by_id),
            rootNodes=len(model.root_ids),
            byKind=kind_counts,
        ),
        relationships=ModelRelationshipMetrics(
            hierarchyEdges=hierarchy_edges,
            compositionEdges=composition_edges,
            graphEdges=graph_edges,
            uniqueGraphRelationshipNames=len(model.graph_relationship_names),
            byRelationshipName=relationship_name_counts,
        ),
        quality=ModelQualityMetrics(
            confidence=confidence_counts,
            semanticRole=semantic_role_counts,
            lowConfidenceNodes=confidence_counts.get("low", 0),
            unknownSemanticRoleNodes=semantic_role_counts.get("unknown", 0),
        ),
        coverage=ModelCoverageMetrics(
            readableProperties=len(model.property_to_node),
            invokableActions=len(model.action_to_method),
            typedInstanceGroups=len(model.instances_by_type_id),
            typedInstances=typed_instances,
        ),
        context=ModelContextMetrics(
            namespaceCounts=namespace_counts,
            nodesWithoutNamespace=nodes_without_namespace,
            appliedProfileCounts=applied_profile_counts,
            nodesWithoutProfiles=nodes_without_profiles,
        ),
    )


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
