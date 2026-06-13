from __future__ import annotations

import asyncio
import base64
import http
import json
import logging
import os
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from time import perf_counter
from typing import Any, Generic, TypeVar

from asyncua import ua
from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from i3x_server.dependencies import get_opcua_client, get_or_build_model, get_subscription_service
from i3x_server.errors import i3x_http_error
from i3x_server.opcua.client import (
    OpcUaClientProtocol,
    OpcUaNamespaceInfo,
    OpcUaObjectTypeInfo,
)
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.objecttype_schema import (
    build_data_type_schema,
    build_object_type_schema,
    json_schema_for_opcua_type,
)
from i3x_server.schemas.state import BuildResult
from i3x_server.subscriptions.service import SubscriptionService
from i3x_server.version import get_server_version

router = APIRouter(prefix="/v1", tags=["v1"])
logger = logging.getLogger(__name__)

T = TypeVar("T")

_UA_BUILTIN_DATATYPE_NAMES: dict[int, str] = {
    1: "Boolean",
    2: "SByte",
    3: "Byte",
    4: "Int16",
    5: "UInt16",
    6: "Int32",
    7: "UInt32",
    8: "Int64",
    9: "UInt64",
    10: "Float",
    11: "Double",
    12: "String",
    13: "DateTime",
    14: "Guid",
    15: "ByteString",
    16: "XmlElement",
    17: "NodeId",
    18: "ExpandedNodeId",
    19: "StatusCode",
    20: "QualifiedName",
    21: "LocalizedText",
    22: "ExtensionObject",
    23: "DataValue",
    24: "Variant",
    25: "DiagnosticInfo",
}

_UA_STANDARD_NON_DATATYPE_TYPE_NAMES: set[str] = {
    "BaseObjectType",
    "FolderType",
    "BaseVariableType",
    "BaseDataVariableType",
    "PropertyType",
    "DataTypeDescriptionType",
    "DataTypeDictionaryType",
    "DataTypeSystemType",
    "DataTypeEncodingType",
    "ModellingRuleType",
    "NamingRuleType",
}

_UA_STANDARD_INTEGER_OPTIONSET_TYPE_NAMES: set[str] = {
    "PermissionType",
    "AccessRestrictionType",
}

_UA_STANDARD_OBJECT_FALLBACK_TYPE_NAMES: set[str] = {
    "Range",
    "EUInformation",
    "Annotation",
    "RolePermissionType",
}

_ENABLE_LIVE_TYPE_NAME_LOOKUP = os.getenv("I3X_ENABLE_TYPE_BROWSENAME_LOOKUP", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
_LIVE_TYPE_NAME_LOOKUP_TIMEOUT_S = float(os.getenv("I3X_TYPE_BROWSENAME_LOOKUP_TIMEOUT_S", "0.05"))
_LIVE_TYPE_NAME_LOOKUP_MAX_PER_REQUEST = int(os.getenv("I3X_TYPE_BROWSENAME_LOOKUP_MAX", "20"))


class SuccessResponse(BaseModel, Generic[T]):
    success: bool = True
    result: T | None = None


class ErrorDetail(BaseModel):
    code: int
    message: str


class ResponseDetail(BaseModel):
    title: str
    status: int
    detail: str


class BulkResultItem(BaseModel, Generic[T]):
    success: bool = True
    elementId: str | None = None
    subscriptionId: str | None = None
    result: T | None = None
    error: ErrorDetail | None = None
    responseDetail: ResponseDetail | None = None


class BulkResponse(BaseModel, Generic[T]):
    success: bool = True
    results: list[BulkResultItem[T]] = Field(default_factory=list)


def _bulk_response(results: list[BulkResultItem[T]]) -> BulkResponse[T]:
    for item in results:
        if item.success or item.responseDetail is not None:
            continue
        error_code = item.error.code if item.error is not None else 500
        error_message = item.error.message if item.error is not None else "Request failed"
        item.responseDetail = ResponseDetail(
            title=_status_title(error_code),
            status=error_code,
            detail=error_message,
        )
    return BulkResponse(success=all(item.success for item in results), results=results)


@dataclass(slots=True)
class _ObjectTypeContext:
    namespace_infos: list[OpcUaNamespaceInfo]
    object_types: list[OpcUaObjectTypeInfo]
    element_ids_by_node_id: dict[str, str]
    items: list[ObjectTypeResponse]
    source_type_to_element_id: dict[str, str]


class QueryCapabilities(BaseModel):
    history: bool


class UpdateCapabilities(BaseModel):
    current: bool
    history: bool


class SubscribeCapabilities(BaseModel):
    stream: bool


class ServerCapabilities(BaseModel):
    query: QueryCapabilities
    update: UpdateCapabilities
    subscribe: SubscribeCapabilities


class ServerInfo(BaseModel):
    specVersion: str
    serverVersion: str | None = None
    serverName: str | None = None
    capabilities: ServerCapabilities


class Namespace(BaseModel):
    uri: str
    displayName: str


class ObjectTypeResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    elementId: str
    displayName: str
    namespaceUri: str
    sourceTypeId: str
    version: str | None = None
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")
    related: dict[str, Any] | None = None


class ObjectInstanceMetadata(BaseModel):
    typeNamespaceUri: str | None = None
    sourceTypeId: str | None = None
    description: str | None = None
    compositionParentId: str | None = None
    relationships: dict[str, Any] | None = None
    extendedAttributes: dict[str, Any] | None = None
    system: dict[str, Any] | None = None


class ObjectInstanceResponse(BaseModel):
    elementId: str
    displayName: str
    typeElementId: str
    parentId: str | None = None
    isComposition: bool
    isExtended: bool = False
    metadata: ObjectInstanceMetadata | None = None


class RelationshipType(BaseModel):
    elementId: str
    displayName: str
    namespaceUri: str
    relationshipId: str
    reverseOf: str


class VQT(BaseModel):
    value: Any
    quality: str
    timestamp: str


class CurrentValueResult(BaseModel):
    isComposition: bool
    value: Any
    quality: str
    timestamp: str
    components: dict[str, VQT] | None = None


class HistoricalValueResult(BaseModel):
    isComposition: bool
    values: list[VQT] = Field(default_factory=list)


class RelatedObjectResult(BaseModel):
    sourceRelationship: str
    object: ObjectInstanceResponse


class GetObjectTypesRequest(BaseModel):
    elementIds: list[str]


class GetRelationshipTypesRequest(BaseModel):
    elementIds: list[str]


class GetObjectsRequest(BaseModel):
    elementIds: list[str]
    includeMetadata: bool = False


class GetRelatedObjectsRequest(BaseModel):
    elementIds: list[str]
    relationshipType: str | None = None
    includeMetadata: bool = False


class GetObjectValueRequest(BaseModel):
    elementIds: list[str]
    maxDepth: int | None = 1


class GetObjectHistoryRequest(BaseModel):
    elementIds: list[str]
    startTime: str
    endTime: str
    maxDepth: int | None = Field(default=1, ge=0)


class RegisterMonitoredItemsRequest(BaseModel):
    clientId: str | None = None
    subscriptionId: str
    elementIds: list[str]
    maxDepth: int | None = 1


class SyncRequest(BaseModel):
    """Request body for polling pending subscription updates."""

    clientId: str | None = None
    subscriptionId: str
    acknowledgeSequence: int | None = Field(
        default=None,
        description=(
            "Last sequence number the client has processed. All updates with a sequence number "
            "<= this value are removed from the pending queue. "
            "Pass -1 to acknowledge and discard all pending updates. "
            "Omit or pass null on the first call to receive all buffered updates without discarding any."
        ),
        validation_alias=AliasChoices("acknowledgeSequence", "lastSequenceNumber"),
    )


class ListSubscriptionsRequest(BaseModel):
    clientId: str | None = None
    subscriptionIds: list[str] = Field(default_factory=list)


class DeleteSubscriptionsRequest(BaseModel):
    clientId: str | None = None
    subscriptionIds: list[str]


class StreamRequest(BaseModel):
    """Request body for opening a Server-Sent Events stream for subscription updates."""

    clientId: str | None = None
    subscriptionId: str
    acknowledgeSequence: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Last sequence number the client has already processed. Updates with a sequence number "
            "<= this value are discarded before the stream starts. "
            "Omit to start streaming from the next unacknowledged update."
        ),
        validation_alias=AliasChoices("acknowledgeSequence", "lastSequenceNumber"),
    )


class CreateSubscriptionRequest(BaseModel):
    clientId: str
    displayName: str | None = None


class CreateSubscriptionResponse(BaseModel):
    subscriptionId: str
    clientId: str
    displayName: str | None = None


class SubscriptionDetail(BaseModel):
    subscriptionId: str
    clientId: str | None = None
    displayName: str | None = None
    monitoredObjects: list[dict[str, Any]] = Field(default_factory=list)
    mode: str | None = None


class SyncUpdate(BaseModel):
    sequenceNumber: int
    elementId: str
    value: Any
    quality: str
    timestamp: str


def _not_implemented(feature: str) -> None:
    raise i3x_http_error(
        501,
        "NotImplemented",
        f"{feature} is not implemented in this server",
        {"feature": feature},
    )


def _supported_capabilities() -> ServerCapabilities:
    return ServerCapabilities(
        query=QueryCapabilities(history=True),
        update=UpdateCapabilities(current=False, history=False),
        subscribe=SubscribeCapabilities(stream=True),
    )


@lru_cache(maxsize=1)
def _server_name_from_openapi(default_name: str = "The i3X API Gateway for OPC UA") -> str:
    openapi_path = Path(__file__).resolve().parents[2] / "openapi.json"
    try:
        openapi_doc = json.loads(openapi_path.read_text(encoding="utf-8"))
        info = openapi_doc.get("info")
        if isinstance(info, Mapping):
            title = info.get("title")
            if isinstance(title, str) and title.strip():
                return title.strip()
    except Exception:
        logger.debug("Failed to read OpenAPI title for server info", exc_info=True)
    return default_name


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
    lower = uri.lower()
    if "cesmii.org/i3x" in lower:
        return "I3X"
    if "isa.org/isa95" in lower:
        return "ISA95"
    if "abelara.com" in lower and lower.rstrip("/").endswith("/equipment"):
        return "Abelara Equipment"
    if "thinkiq.com" in lower and lower.rstrip("/").endswith("/equipment"):
        return "ThinkIQ Equipment"

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

    type_namespace_uri = _namespace_uri_from_expanded_node_id(type_element_id)
    if type_namespace_uri is None:
        resolved_type_namespace_uri = _namespace_uri_for_node_id(type_element_id, namespace_infos)
        if resolved_type_namespace_uri:
            type_namespace_uri = resolved_type_namespace_uri
    if type_namespace_uri is None:
        type_namespace_uri = _namespace_uri_from_expanded_node_id(source_type_id_expanded)
    if type_namespace_uri is None:
        resolved_source_namespace_uri = _namespace_uri_for_node_id(source_type_id, namespace_infos)
        type_namespace_uri = resolved_source_namespace_uri or None
    if type_namespace_uri is not None:
        type_namespace_uri = _canonical_namespace_uri(type_namespace_uri, namespace_infos)

    metadata = None
    if include_metadata:
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
        metadata = ObjectInstanceMetadata(
            typeNamespaceUri=type_namespace_uri,
            sourceTypeId=source_type_id_expanded,
            description=f"Derived from model node {node.name}",
            compositionParentId=composition_parent_id,
            relationships=relationships,
        )
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


def _parse_iso_datetime(value: str, field_name: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise i3x_http_error(
            400,
            "InvalidArgument",
            f"Invalid ISO 8601 timestamp for '{field_name}'",
            {"field": field_name, "value": value},
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_history_time_range(body: GetObjectHistoryRequest) -> tuple[datetime | None, datetime | None]:
    start_time = _parse_iso_datetime(body.startTime, "startTime")
    end_time = _parse_iso_datetime(body.endTime, "endTime")
    if start_time > end_time:
        raise i3x_http_error(
            400,
            "InvalidArgument",
            "startTime must be less than or equal to endTime",
            {"startTime": body.startTime, "endTime": body.endTime},
        )
    return start_time, end_time


def _now_iso() -> str:
    return _format_utc_timestamp(datetime.now(timezone.utc))


def _status_title(status_code: int) -> str:
    try:
        return http.HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


def _format_utc_timestamp(value: datetime) -> str:
    normalized = value
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _to_json_safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return _format_utc_timestamp(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        encoded = base64.b64encode(bytes(value)).decode("ascii")
        return {"encoding": "base64", "data": encoded}
    if isinstance(value, BaseModel):
        return _to_json_safe_value(value.model_dump(mode="json", by_alias=True))
    if is_dataclass(value):
        return {item.name: _to_json_safe_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, (list, tuple, set)):
        return [_to_json_safe_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_json_safe_value(item) for key, item in value.items()}
    if hasattr(value, "__dict__") and type(value).__module__ != "builtins":
        return {
            str(key): _to_json_safe_value(item)
            for key, item in vars(value).items()
            if not key.startswith("_") and not callable(item)
        }
    return str(value)


def _vqt_from_any(value: Any) -> VQT:
    if value is None:
        return VQT(value=None, quality="GoodNoData", timestamp=_now_iso())
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
    items = [
        _to_object_type(item, model, namespace_infos, object_types_by_node_id, element_ids_by_node_id, {})
        for item in object_types
    ]
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
    lookup_budget = {"remaining": _LIVE_TYPE_NAME_LOOKUP_MAX_PER_REQUEST}

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


def _datatype_object_type_from_source_type_id(
    source_type_id: str,
    namespace_infos: list[OpcUaNamespaceInfo],
) -> ObjectTypeResponse | None:
    # Try to resolve the datatype schema from asyncua definitions (covers structures, standard types, etc.)
    schema = build_data_type_schema(source_type_id, namespace_infos)

    # If no structured schema found, fall back to scalar type inference with array variant support
    if not isinstance(schema, Mapping):
        scalar_schema: dict[str, Any] | None = None
        if _is_builtin_ua_datatype_node_id(source_type_id):
            scalar_schema = json_schema_for_opcua_type(source_type_id)
        else:
            scalar_schema = _scalar_schema_for_standard_ua_datatype_node_id(source_type_id)
        if scalar_schema is None:
            return None
        # Wrap scalar schema to allow both single values and arrays
        schema = {
            "oneOf": [
                dict(scalar_schema),
                {"type": "array", "items": dict(scalar_schema)},
            ]
        }

    # Extract metadata for response
    node_id_match = re.match(r"^nsu=[^;]+;i=(\d+)$", source_type_id, flags=re.IGNORECASE)
    numeric_id = int(node_id_match.group(1)) if node_id_match is not None else None
    builtin_id = numeric_id if _is_builtin_ua_datatype_node_id(source_type_id) else None

    namespace_uri = _namespace_uri_from_expanded_node_id(source_type_id)
    if namespace_uri is None:
        namespace_uri = _namespace_uri_for_node_id(source_type_id, namespace_infos)
    if not namespace_uri:
        return None
    namespace_uri = _canonical_namespace_uri(namespace_uri, namespace_infos)

    # Determine display name from schema title or type name lookup
    title = schema.get("title") if isinstance(schema, Mapping) else None
    display_name = title if isinstance(title, str) and title else "StructureType"
    if builtin_id is not None:
        display_name = _UA_BUILTIN_DATATYPE_NAMES.get(builtin_id, display_name)
    elif _is_standard_ua_namespace_node_id(source_type_id):
        standard_name = _standard_ua_type_name(source_type_id)
        if standard_name:
            display_name = standard_name

    # Finalize schema with metadata
    schema_payload = dict(schema)
    schema_payload.setdefault("title", display_name)
    schema_payload.setdefault("x-opcua-nodeId", source_type_id)
    schema_payload.setdefault("x-opcua-displayName", display_name)

    return ObjectTypeResponse(
        elementId=_virtual_object_type_element_id(namespace_uri, display_name, source_type_id),
        displayName=display_name,
        namespaceUri=namespace_uri,
        sourceTypeId=source_type_id,
        schema=schema_payload,
    )


def _opaque_datatype_object_type_from_source_type_id(
    source_type_id: str,
    namespace_infos: list[OpcUaNamespaceInfo],
) -> ObjectTypeResponse | None:
    namespace_uri = _namespace_uri_from_expanded_node_id(source_type_id)
    if namespace_uri is None:
        namespace_uri = _namespace_uri_for_node_id(source_type_id, namespace_infos)
    if not namespace_uri:
        return None
    namespace_uri = _canonical_namespace_uri(namespace_uri, namespace_infos)

    display_name = _standard_ua_type_name(source_type_id)
    if not display_name:
        node_id_match = re.match(r"^nsu=[^;]+;i=(\d+)$", source_type_id, flags=re.IGNORECASE)
        if node_id_match is not None:
            display_name = f"DataType_i_{node_id_match.group(1)}"
        else:
            display_name = "DataType"

    schema_payload = {
        "title": display_name,
        "description": "Fallback schema for unresolved OPC UA DataType",
        "oneOf": [
            {"type": "object"},
            {"type": "array", "items": {"type": "object"}},
            {"type": "number"},
            {"type": "string"},
            {"type": "boolean"},
        ],
        "x-opcua-nodeId": source_type_id,
        "x-opcua-displayName": display_name,
    }

    return ObjectTypeResponse(
        elementId=_virtual_object_type_element_id(namespace_uri, display_name, source_type_id),
        displayName=display_name,
        namespaceUri=namespace_uri,
        sourceTypeId=source_type_id,
        schema=schema_payload,
    )


def _standard_ua_type_name(element_id: str) -> str | None:
    match = re.match(r"^nsu=[^;]+;i=(\d+)$", element_id, flags=re.IGNORECASE)
    if match is None:
        return None
    identifier = int(match.group(1))
    object_id_names = getattr(ua, "ObjectIdNames", None)
    if not isinstance(object_id_names, Mapping):
        return None
    candidate = object_id_names.get(identifier)
    return candidate if isinstance(candidate, str) and candidate else None


def _scalar_schema_for_standard_ua_datatype_node_id(element_id: str) -> dict[str, Any] | None:
    if not _is_standard_ua_namespace_node_id(element_id):
        return None

    name = _standard_ua_type_name(element_id)
    if not name:
        return None

    if name in _UA_STANDARD_NON_DATATYPE_TYPE_NAMES:
        return None

    if name in _UA_STANDARD_INTEGER_OPTIONSET_TYPE_NAMES:
        return {"type": "integer"}

    if name in _UA_STANDARD_OBJECT_FALLBACK_TYPE_NAMES:
        return {"type": "object"}

    if name.endswith("DataType"):
        return {"type": "object"}

    if name.endswith("Type"):
        # Standard namespace "...Type" symbols are generally type-like definitions.
        return {"type": "object"}

    if name.endswith("State") or name.endswith("Enumeration") or name.endswith("Enum"):
        return {"type": "integer"}

    if "_" in name:
        # Exclude standard method/browse aliases such as Server_GetMonitoredItems.
        return None

    if name.endswith("ObjectType") or name.endswith("VariableType") or name.endswith("ReferenceType"):
        return None

    inferred = json_schema_for_opcua_type(name)
    if inferred == {"type": "string"}:
        # Avoid treating arbitrary standard NodeIds as scalar datatypes when only
        # the default string fallback can be inferred.
        return None
    return inferred


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
        _ENABLE_LIVE_TYPE_NAME_LOOKUP
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
                        timeout=_LIVE_TYPE_NAME_LOOKUP_TIMEOUT_S,
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
    raise i3x_http_error(
        400,
        "InvalidArgument",
        f"'{endpoint}' requires a non-empty clientId",
        {"field": "clientId"},
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


@router.get("/info", response_model=SuccessResponse[ServerInfo])
async def get_info() -> SuccessResponse[ServerInfo]:
    return SuccessResponse(result=_build_server_info())


@router.get("/namespaces", response_model=SuccessResponse[list[Namespace]])
async def get_namespaces_v1(
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> SuccessResponse[list[Namespace]]:
    try:
        namespace_infos = await opcua_client.get_namespace_infos()
    except Exception as exc:
        raise i3x_http_error(
            502,
            "OpcUaNamespaceError",
            "Failed to read OPC UA namespaces",
            {"cause": str(exc)},
        ) from exc
    return SuccessResponse(result=[_to_namespace(item) for item in namespace_infos])


@router.get("/objecttypes", response_model=SuccessResponse[list[ObjectTypeResponse]])
async def get_object_types_v1(
    request: Request,
    namespace_uri: str | None = Query(default=None, alias="namespaceUri"),
    model: BuildResult = Depends(get_or_build_model),
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> SuccessResponse[list[ObjectTypeResponse]]:
    try:
        context = await _get_object_type_context(request, model, opcua_client)
    except Exception as exc:
        raise i3x_http_error(
            502,
            "OpcUaObjectTypesError",
            "Failed to read OPC UA object types",
            {"cause": str(exc)},
        ) from exc
    namespace_infos = context.namespace_infos
    items = list(context.items)

    if namespace_uri:
        canonical_filter = _canonical_namespace_uri(namespace_uri, namespace_infos)
        items = [item for item in items if item.namespaceUri == canonical_filter]
    return SuccessResponse(result=items)


@router.post("/objecttypes/query", response_model=BulkResponse[ObjectTypeResponse])
async def query_object_types_v1(
    request: Request,
    body: GetObjectTypesRequest,
    model: BuildResult = Depends(get_or_build_model),
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> BulkResponse[ObjectTypeResponse]:
    try:
        context = await _get_object_type_context(request, model, opcua_client)
    except Exception as exc:
        raise i3x_http_error(
            502,
            "OpcUaObjectTypesError",
            "Failed to query OPC UA object types",
            {"cause": str(exc)},
        ) from exc
    listed_types = context.items

    indexed = {item.elementId: item for item in listed_types}
    results: list[BulkResultItem[ObjectTypeResponse]] = []
    for element_id in body.elementIds:
        match = indexed.get(element_id)
        if match is None:
            results.append(
                BulkResultItem[ObjectTypeResponse](
                    success=False,
                    elementId=element_id,
                    error=ErrorDetail(code=404, message="Object type not found"),
                )
            )
        else:
            results.append(BulkResultItem[ObjectTypeResponse](success=True, elementId=element_id, result=match))
    return _bulk_response(results)


@router.get("/relationshiptypes", response_model=SuccessResponse[list[RelationshipType]])
async def get_relationship_types(
    namespace_uri: str | None = Query(default=None, alias="namespaceUri"),
    model: BuildResult = Depends(get_or_build_model),
) -> SuccessResponse[list[RelationshipType]]:
    items = _relationship_type_items(model)
    if namespace_uri is not None:
        items = [item for item in items if item.namespaceUri == namespace_uri]
    return SuccessResponse(result=items)


@router.post("/relationshiptypes/query", response_model=BulkResponse[RelationshipType])
async def query_relationship_types(
    body: GetRelationshipTypesRequest,
    model: BuildResult = Depends(get_or_build_model),
) -> BulkResponse[RelationshipType]:
    items = {item.elementId: item for item in _relationship_type_items(model)}
    results: list[BulkResultItem[RelationshipType]] = []
    for element_id in body.elementIds:
        match = items.get(element_id)
        if match is None:
            results.append(
                BulkResultItem[RelationshipType](
                    success=False,
                    elementId=element_id,
                    error=ErrorDetail(code=404, message="Relationship type not found"),
                )
            )
        else:
            results.append(BulkResultItem[RelationshipType](success=True, elementId=element_id, result=match))
    return _bulk_response(results)


@router.get("/objects", response_model=SuccessResponse[list[ObjectInstanceResponse]])
async def get_objects_v1(
    request: Request,
    type_element_id: str | None = Query(default=None, alias="typeElementId"),
    include_metadata: bool = Query(default=False, alias="includeMetadata"),
    root: bool | None = Query(default=None),
    model: BuildResult = Depends(get_or_build_model),
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> SuccessResponse[list[ObjectInstanceResponse]]:
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

    if root is True:
        hierarchy_parent_ids = set()
        raw_hierarchy_parent_by_id = getattr(model, "hierarchy_parent_by_id", None)
        if isinstance(raw_hierarchy_parent_by_id, dict):
            hierarchy_parent_ids = {
                node_id for node_id in raw_hierarchy_parent_by_id.keys() if isinstance(node_id, str)
            }
        composition_parent_ids = set()
        raw_composition_parent_by_id = getattr(model, "composition_parent_by_id", None)
        if isinstance(raw_composition_parent_by_id, dict):
            composition_parent_ids = {
                node_id for node_id in raw_composition_parent_by_id.keys() if isinstance(node_id, str)
            }
        nodes = [
            node
            for node in model.nodes_by_id.values()
            if (
                node.id not in hierarchy_parent_ids
                and node.id not in composition_parent_ids
                and node.kind in {"asset", "eventSource"}
            )
        ]
        if not nodes:
            nodes = [model.nodes_by_id[node_id] for node_id in model.root_ids if node_id in model.nodes_by_id]
    else:
        nodes = list(model.nodes_by_id.values())
    if type_element_id is not None:
        nodes = [
            node
            for node in nodes
            if node.type == type_element_id
            or node.kind == type_element_id
            or _resolved_type_element_id_for_node(
                node,
                namespace_infos,
                object_type_element_ids_by_node_id,
                object_type_element_ids_by_source_type,
            )
            == type_element_id
        ]
    return SuccessResponse(
        result=[
            _to_object_instance(
                model,
                node,
                include_metadata,
                namespace_infos,
                object_type_element_ids_by_node_id,
                object_type_element_ids_by_source_type,
            )
            for node in nodes
        ]
    )


@router.post("/objects/list", response_model=BulkResponse[ObjectInstanceResponse])
async def list_objects_by_id_v1(
    request: Request,
    body: GetObjectsRequest,
    model: BuildResult = Depends(get_or_build_model),
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> BulkResponse[ObjectInstanceResponse]:
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

    results: list[BulkResultItem[ObjectInstanceResponse]] = []
    for element_id in body.elementIds:
        node = _find_model_node(model, element_id)
        if node is None:
            results.append(
                BulkResultItem[ObjectInstanceResponse](
                    success=False,
                    elementId=element_id,
                    error=ErrorDetail(code=404, message="Object not found"),
                )
            )
            continue
        results.append(
            BulkResultItem[ObjectInstanceResponse](
                success=True,
                elementId=element_id,
                result=_to_object_instance(
                    model,
                    node,
                    include_metadata=body.includeMetadata,
                    namespace_infos=namespace_infos,
                    object_type_element_ids_by_node_id=object_type_element_ids_by_node_id,
                    object_type_element_ids_by_source_type=object_type_element_ids_by_source_type,
                ),
            )
        )
    return _bulk_response(results)


@router.post(
    "/objects/related",
    response_model=BulkResponse[list[RelatedObjectResult]],
    summary="Query related objects",
    description=(
        "Return all objects related to the requested elements across all relationship planes: "
        "hierarchy (`HasChildren`, `HasParent`), composition (`HasComponent`, `ComponentOf`), "
        "and graph (custom/non-hierarchical). "
        "Use `relationshipType` to filter results to a specific named relationship. "
        "Failing items include an item-level `responseDetail` alongside `error`."
    ),
)
async def query_related_objects_v1(
    request: Request,
    body: GetRelatedObjectsRequest,
    model: BuildResult = Depends(get_or_build_model),
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> BulkResponse[list[RelatedObjectResult]]:
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

    results: list[BulkResultItem[list[RelatedObjectResult]]] = []
    for element_id in body.elementIds:
        node = _find_model_node(model, element_id)
        if node is None:
            results.append(
                BulkResultItem[list[RelatedObjectResult]](
                    success=False,
                    elementId=element_id,
                    error=ErrorDetail(code=404, message="Object not found"),
                )
            )
            continue
        related: list[RelatedObjectResult] = []
        relationship_map = _relationships_for_node(model, node)
        for relationship_name, target_ids in relationship_map.items():
            relationship = _relationship_type_for_name(relationship_name, node)
            if body.relationshipType is not None and relationship.elementId != body.relationshipType:
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
                            include_metadata=body.includeMetadata,
                            namespace_infos=namespace_infos,
                            object_type_element_ids_by_node_id=object_type_element_ids_by_node_id,
                            object_type_element_ids_by_source_type=object_type_element_ids_by_source_type,
                        ),
                    )
                )
        results.append(
            BulkResultItem[list[RelatedObjectResult]](
                success=True,
                elementId=element_id,
                result=related,
            )
        )
    return _bulk_response(results)


@router.post(
    "/objects/value",
    response_model=BulkResponse[CurrentValueResult],
    summary="Query last known values",
    description=(
        "Return the last known value, quality, and timestamp for one or more objects. "
        "Quality and timestamp are sourced directly from the OPC UA server; quality values are "
        "`Good`, `Uncertain`, or `Bad`. "
        "A null value with `Good` or `Uncertain` quality is normalized to `GoodNoData`. "
        "When `maxDepth > 1`, component values are recursed using the **composition** adjacency only — "
        "hierarchy-only children are never included. "
        "Failing items include an item-level `responseDetail` alongside `error`."
    ),
)
async def query_last_known_values_v1(
    body: GetObjectValueRequest,
    model: BuildResult = Depends(get_or_build_model),
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> BulkResponse[CurrentValueResult]:
    node_ids: list[str] = []
    ordered_nodes: list[tuple[str, ModelNode]] = []
    component_nodes_by_element_id: dict[str, list[ModelNode]] = {}
    requested_depth = body.maxDepth if body.maxDepth is not None else 1
    for element_id in body.elementIds:
        node = _find_model_node(model, element_id)
        if node is None:
            continue
        if node.kind == "property":
            node_ids.append(node.source_node_id)
        ordered_nodes.append((element_id, node))
        component_nodes = _collect_value_component_nodes(model, node, requested_depth)
        component_nodes_by_element_id[element_id] = component_nodes
        node_ids.extend(item.source_node_id for item in component_nodes)

    values_by_node_id: dict[str, Any] = {}
    if node_ids:
        try:
            raw_data_values = await opcua_client.read_data_values(node_ids)
            values_by_node_id = {node_id: dv for node_id, dv in zip(node_ids, raw_data_values, strict=False)}
        except Exception as exc:
            raise i3x_http_error(
                502,
                "OpcUaReadError",
                "Failed to read OPC UA values",
                {"cause": str(exc)},
            ) from exc

    results: list[BulkResultItem[CurrentValueResult]] = []
    lookup_by_element_id = {element_id: node for element_id, node in ordered_nodes}
    for element_id in body.elementIds:
        node = lookup_by_element_id.get(element_id)
        if node is None:
            results.append(
                BulkResultItem[CurrentValueResult](
                    success=False,
                    elementId=element_id,
                    error=ErrorDetail(code=404, message=f"Element not found: {element_id}"),
                )
            )
            continue

        root_vqt = (
            _vqt_from_data_value(values_by_node_id[node.source_node_id])
            if node.source_node_id in values_by_node_id
            else VQT(value=None, quality="GoodNoData", timestamp=_now_iso())
        )
        component_nodes = component_nodes_by_element_id.get(element_id, [])
        components: dict[str, VQT] = {}
        for component_node in component_nodes:
            comp_dv = values_by_node_id.get(component_node.source_node_id)
            components[component_node.id] = (
                _vqt_from_data_value(comp_dv)
                if comp_dv is not None
                else VQT(value=None, quality="GoodNoData", timestamp=_now_iso())
            )

        result = CurrentValueResult(
            isComposition=bool(_composition_children_for_node(model, node)),
            value=root_vqt.value,
            quality=root_vqt.quality,
            timestamp=root_vqt.timestamp,
            components=components or None,
        )
        results.append(BulkResultItem[CurrentValueResult](success=True, elementId=element_id, result=result))
    return _bulk_response(results)


@router.post(
    "/objects/history",
    response_model=BulkResponse[HistoricalValueResult],
    summary="Query historical values",
    description=(
        "Return historical values for one or more objects within the specified time range. "
        "Values are ordered by source timestamp ascending. "
        "Component recursion follows the **composition** adjacency only — hierarchy-only children are excluded. "
        "Failing items include an item-level `responseDetail` alongside `error`."
    ),
)
async def query_historical_values_v1(
    body: GetObjectHistoryRequest,
    model: BuildResult = Depends(get_or_build_model),
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> BulkResponse[HistoricalValueResult]:
    start_time, end_time = _parse_history_time_range(body)
    max_depth = body.maxDepth if body.maxDepth is not None else 1

    lookup: list[tuple[str, ModelNode, list[ModelNode]]] = []
    node_ids: list[str] = []
    for element_id in body.elementIds:
        node = _find_model_node(model, element_id)
        if node is None:
            continue
        source_nodes = _collect_history_source_nodes(model, node, max_depth)
        lookup.append((element_id, node, source_nodes))
        for source_node in source_nodes:
            node_ids.append(source_node.source_node_id)

    values_by_node_id: dict[str, list[Any]] = {}
    if node_ids:
        unique_node_ids = list(dict.fromkeys(node_ids))
        try:
            values_by_node_id = await opcua_client.read_history_values(
                node_ids=unique_node_ids,
                start_time=start_time,
                end_time=end_time,
            )
        except Exception as exc:
            raise i3x_http_error(
                502,
                "OpcUaHistoryReadError",
                "Failed to read OPC UA historical values",
                {"cause": str(exc)},
            ) from exc

    lookup_by_element_id = {element_id: (node, source_nodes) for element_id, node, source_nodes in lookup}

    results: list[BulkResultItem[HistoricalValueResult]] = []
    for element_id in body.elementIds:
        match = lookup_by_element_id.get(element_id)
        if match is None:
            results.append(
                BulkResultItem[HistoricalValueResult](
                    success=False,
                    elementId=element_id,
                    error=ErrorDetail(code=404, message="Object not found"),
                )
            )
            continue

        node, source_nodes = match
        if not source_nodes:
            results.append(
                BulkResultItem[HistoricalValueResult](
                    success=False,
                    elementId=element_id,
                    error=ErrorDetail(code=404, message="Object history not found"),
                )
            )
            continue

        values: list[VQT] = []
        for source_node in source_nodes:
            raw_values = values_by_node_id.get(source_node.source_node_id, [])
            values.extend(_to_vqt_from_history_value(item) for item in raw_values)

        values.sort(key=lambda item: item.timestamp)
        results.append(
            BulkResultItem[HistoricalValueResult](
                success=True,
                elementId=element_id,
                result=HistoricalValueResult(
                    isComposition=bool(_composition_children_for_node(model, node)),
                    values=values,
                ),
            )
        )

    return _bulk_response(results)


@router.get("/objects/{element_id}/history")
async def get_historical_values_v1(element_id: str) -> None:
    _not_implemented(f"Historical values for '{element_id}'")


@router.put("/objects/{element_id}/history")
async def update_object_history_v1(element_id: str) -> None:
    _not_implemented(f"Historical value updates for '{element_id}'")


@router.put("/objects/{element_id}/value")
async def update_object_value_v1(element_id: str) -> None:
    _not_implemented(f"Value update for '{element_id}'")


@router.post("/subscriptions")
async def create_subscription_v1(
    body: CreateSubscriptionRequest,
    subscription_service: SubscriptionService = Depends(get_subscription_service),
) -> SuccessResponse[CreateSubscriptionResponse]:
    created = await subscription_service.create_subscription(
        client_id=body.clientId,
        display_name=body.displayName,
    )
    return SuccessResponse(
        result=CreateSubscriptionResponse(
            subscriptionId=created.subscription_id,
            clientId=created.client_id or body.clientId,
            displayName=created.display_name,
        )
    )


@router.post("/subscriptions/register")
async def register_monitored_items_v1(
    body: RegisterMonitoredItemsRequest,
    model: BuildResult = Depends(get_or_build_model),
    subscription_service: SubscriptionService = Depends(get_subscription_service),
) -> BulkResponse[None]:
    client_id = _require_client_id(body.clientId, "/subscriptions/register")
    max_depth = body.maxDepth or 1
    known_ids: list[str] = []
    results: list[BulkResultItem[None]] = []
    for element_id in body.elementIds:
        if _find_model_node(model, element_id) is None:
            results.append(
                BulkResultItem[None](
                    success=False,
                    elementId=element_id,
                    error=ErrorDetail(code=404, message=f"Element not found: {element_id}"),
                )
            )
            continue
        known_ids.append(element_id)
        results.append(BulkResultItem[None](success=True, elementId=element_id, result=None))

    ok = await subscription_service.register_items(
        client_id=client_id,
        subscription_id=body.subscriptionId,
        element_ids=known_ids,
        max_depth=max_depth,
        model=model,
    )
    if not ok:
        raise i3x_http_error(
            404,
            "SubscriptionNotFound",
            f"Subscription '{body.subscriptionId}' not found",
        )
    return _bulk_response(results)


@router.post("/subscriptions/unregister")
async def remove_monitored_items_v1(
    body: RegisterMonitoredItemsRequest,
    model: BuildResult = Depends(get_or_build_model),
    subscription_service: SubscriptionService = Depends(get_subscription_service),
) -> BulkResponse[None]:
    client_id = _require_client_id(body.clientId, "/subscriptions/unregister")
    known_ids: list[str] = []
    results: list[BulkResultItem[None]] = []
    for element_id in body.elementIds:
        if _find_model_node(model, element_id) is None:
            results.append(
                BulkResultItem[None](
                    success=False,
                    elementId=element_id,
                    error=ErrorDetail(code=404, message=f"Element not found: {element_id}"),
                )
            )
            continue
        known_ids.append(element_id)
        results.append(BulkResultItem[None](success=True, elementId=element_id, result=None))

    ok = await subscription_service.unregister_items(
        client_id=client_id,
        subscription_id=body.subscriptionId,
        element_ids=known_ids,
        model=model,
    )
    if not ok:
        raise i3x_http_error(
            404,
            "SubscriptionNotFound",
            f"Subscription '{body.subscriptionId}' not found",
        )
    return _bulk_response(results)


@router.post(
    "/subscriptions/stream",
    summary="Stream subscription updates via SSE",
    description=(
        "Open a Server-Sent Events (SSE) stream for a subscription. "
        "Each `data:` event carries a JSON array of updates with `sequenceNumber`, "
        "`elementId`, `value`, `quality`, and `timestamp`. "
        "A `: connected` comment is sent immediately on connection. "
        "A `: keepalive` comment is sent periodically when there are no new updates. "
        "An `event: close` message is sent when the stream is terminated server-side. "
        "While a stream is active, `POST /subscriptions/sync` will return HTTP 400 for the same subscription. "
        "Opening a new stream for the same subscription closes the prior stream generation."
    ),
)
async def stream_subscription_v1(
    body: StreamRequest,
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
    subscription_service: SubscriptionService = Depends(get_subscription_service),
) -> StreamingResponse:
    client_id = _require_client_id(body.clientId, "/subscriptions/stream")
    namespace_infos: list[OpcUaNamespaceInfo] = []
    try:
        namespace_infos = await opcua_client.get_namespace_infos()
    except Exception:
        # Streaming should still work even if namespace metadata is temporarily unavailable.
        namespace_infos = []

    stream_generation = await subscription_service.activate_stream(
        client_id=client_id,
        subscription_id=body.subscriptionId,
    )
    if stream_generation is None:
        raise i3x_http_error(
            404,
            "SubscriptionNotFound",
            f"Subscription '{body.subscriptionId}' not found",
        )

    acknowledged = await subscription_service.sync(
        client_id=client_id,
        subscription_id=body.subscriptionId,
        acknowledge_sequence=body.acknowledgeSequence,
        allow_when_stream_active=True,
    )
    if acknowledged is None:
        raise i3x_http_error(
            404,
            "SubscriptionNotFound",
            f"Subscription '{body.subscriptionId}' not found",
        )

    async def event_stream() -> Any:
        try:
            # Send an immediate SSE comment so clients can confirm stream establishment quickly.
            yield ": connected\n\n"
            last_sequence = body.acknowledgeSequence or 0
            while True:
                is_active = await subscription_service.is_stream_active(body.subscriptionId, stream_generation)
                if not is_active:
                    yield "event: close\ndata: {}\n\n"
                    return

                updates = await subscription_service.wait_for_updates(
                    client_id=client_id,
                    subscription_id=body.subscriptionId,
                    after_sequence=last_sequence,
                    timeout_seconds=15,
                )

                if updates is None:
                    yield "event: close\ndata: {}\n\n"
                    return

                if not updates:
                    yield ": keepalive\n\n"
                    continue

                last_sequence = updates[-1].sequence_number
                payload = [
                    {
                        "sequenceNumber": item.sequence_number,
                        "elementId": _expanded_node_id(item.element_id, namespace_infos),
                        "value": _to_json_safe_value(item.value),
                        "quality": item.quality,
                        "timestamp": item.timestamp,
                    }
                    for item in updates
                ]
                encoded_payload = jsonable_encoder(payload)
                yield f"data: {json.dumps(encoded_payload)}\n\n"
        finally:
            await subscription_service.deactivate_stream(body.subscriptionId, stream_generation)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/subscriptions/sync",
    summary="Sync subscription updates",
    description=(
        "Return all pending updates for a subscription and acknowledge previously received ones. "
        "Set `acknowledgeSequence` to the last sequence number the client processed to discard "
        "older entries from the queue. "
        "Pass `acknowledgeSequence=-1` to acknowledge and discard **all** pending updates. "
        "Returns HTTP 206 with a `responseDetail` if updates were dropped due to queue overflow since the last sync. "
        "Returns HTTP 400 if the subscription has an active SSE stream — close the stream before calling sync."
    ),
)
async def sync_subscription_v1(
    body: SyncRequest,
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
    subscription_service: SubscriptionService = Depends(get_subscription_service),
) -> Any:
    client_id = _require_client_id(body.clientId, "/subscriptions/sync")
    namespace_infos: list[OpcUaNamespaceInfo] = []
    try:
        namespace_infos = await opcua_client.get_namespace_infos()
    except Exception:
        namespace_infos = []

    active_stream = await subscription_service.has_active_stream(
        client_id=client_id,
        subscription_id=body.subscriptionId,
    )
    if active_stream is None:
        raise i3x_http_error(
            404,
            "SubscriptionNotFound",
            f"Subscription '{body.subscriptionId}' not found",
        )
    if active_stream:
        raise i3x_http_error(
            400,
            "BadRequest",
            "Subscription has an active stream; close stream before calling sync",
        )

    synced = await subscription_service.sync(
        client_id=client_id,
        subscription_id=body.subscriptionId,
        acknowledge_sequence=body.acknowledgeSequence,
    )
    if synced is None:
        raise i3x_http_error(
            404,
            "SubscriptionNotFound",
            f"Subscription '{body.subscriptionId}' not found",
        )
    result_payload = [
        SyncUpdate(
            sequenceNumber=item.sequence_number,
            elementId=_expanded_node_id(item.element_id, namespace_infos),
            value=_to_json_safe_value(item.value),
            quality=item.quality,
            timestamp=item.timestamp,
        ).model_dump(mode="json")
        for item in synced.updates
    ]

    if synced.queue_overflow:
        detail = (
            "Updates were dropped from the subscription queue. "
            f"Dropped sequence numbers {synced.dropped_from_sequence} through {synced.dropped_to_sequence}."
        )
        return JSONResponse(
            status_code=206,
            content={
                "success": True,
                "result": result_payload,
                "responseDetail": {
                    "title": "Updates dropped due to queue overflow",
                    "status": 206,
                    "detail": detail,
                },
            },
        )

    return SuccessResponse(result=result_payload)


@router.post("/subscriptions/delete")
async def delete_subscriptions_v1(
    body: DeleteSubscriptionsRequest,
    subscription_service: SubscriptionService = Depends(get_subscription_service),
) -> BulkResponse[None]:
    client_id = _require_client_id(body.clientId, "/subscriptions/delete")
    deleted = await subscription_service.delete_subscriptions(client_id, body.subscriptionIds)
    return _bulk_response(
        [
            BulkResultItem[None](
                success=item.success,
                subscriptionId=item.subscription_id,
                result=None,
                error=None if item.error is None else ErrorDetail(**item.error),
            )
            for item in deleted
        ]
    )


@router.post("/subscriptions/list")
async def list_subscriptions_v1(
    body: ListSubscriptionsRequest,
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
    subscription_service: SubscriptionService = Depends(get_subscription_service),
) -> BulkResponse[SubscriptionDetail]:
    client_id = _require_client_id(body.clientId, "/subscriptions/list")
    namespace_infos: list[OpcUaNamespaceInfo] = []
    try:
        namespace_infos = await opcua_client.get_namespace_infos()
    except Exception:
        namespace_infos = []

    filter_ids = body.subscriptionIds or None
    subscriptions = await subscription_service.list_subscriptions(client_id, filter_ids)
    found: dict[str, SubscriptionDetail] = {
        item.subscription_id: SubscriptionDetail(
            subscriptionId=item.subscription_id,
            clientId=item.client_id,
            displayName=item.display_name,
            monitoredObjects=[
                {
                    **monitored,
                    "elementId": _expanded_node_id(str(monitored.get("elementId", "")), namespace_infos),
                }
                for monitored in item.monitored_objects
            ],
            mode=item.mode,
        )
        for item in subscriptions
    }

    if filter_ids is None:
        return _bulk_response(
            [
                BulkResultItem[SubscriptionDetail](
                    success=True,
                    elementId=item.subscription_id,
                    subscriptionId=item.subscription_id,
                    result=detail,
                )
                for item in subscriptions
                for detail in [found[item.subscription_id]]
            ]
        )

    results: list[BulkResultItem[SubscriptionDetail]] = []
    for subscription_id in body.subscriptionIds:
        detail = found.get(subscription_id)
        if detail is None:
            results.append(
                BulkResultItem[SubscriptionDetail](
                    success=False,
                    elementId=subscription_id,
                    subscriptionId=subscription_id,
                    error=ErrorDetail(code=404, message=f"Subscription not found: {subscription_id}"),
                )
            )
            continue
        results.append(
            BulkResultItem[SubscriptionDetail](
                success=True,
                elementId=subscription_id,
                subscriptionId=subscription_id,
                result=detail,
            )
        )
    return _bulk_response(results)
