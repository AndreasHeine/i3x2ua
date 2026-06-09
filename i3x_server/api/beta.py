from __future__ import annotations

import re
import json
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from i3x_server.dependencies import get_opcua_client, get_or_build_model, get_subscription_service
from i3x_server.errors import i3x_http_error
from i3x_server.opcua.client import (
    OpcUaClientProtocol,
    OpcUaNamespaceInfo,
    OpcUaObjectTypeInfo,
)
from i3x_server.schemas.objecttype_schema import build_object_type_schema
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult
from i3x_server.subscriptions.service import SubscriptionService

router = APIRouter(prefix="/v1", tags=["beta"])

T = TypeVar("T")


class SuccessResponse(BaseModel, Generic[T]):
    success: bool = True
    result: T | None = None


class ErrorDetail(BaseModel):
    code: int
    message: str


class BulkResultItem(BaseModel, Generic[T]):
    success: bool = True
    elementId: str | None = None
    subscriptionId: str | None = None
    result: T | None = None
    error: ErrorDetail | None = None


class BulkResponse(BaseModel, Generic[T]):
    success: bool = True
    results: list[BulkResultItem[T]] = Field(default_factory=list)


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
    elementId: str
    value: VQT | None = None


class RelatedObjectResult(BaseModel):
    sourceRelationship: str
    object: ObjectInstanceResponse


class GetObjectTypesRequest(BaseModel):
    elementIds: list[str]


class GetRelationshipTypesRequest(BaseModel):
    elementIds: list[str]


class GetObjectsRequest(BaseModel):
    elementIds: list[str]


class GetRelatedObjectsRequest(BaseModel):
    elementIds: list[str]
    relationshipType: str | None = None
    includeMetadata: bool = False


class GetObjectValueRequest(BaseModel):
    elementIds: list[str]
    maxDepth: int | None = 1


class GetObjectHistoryRequest(BaseModel):
    elementIds: list[str]
    startTime: str | None = None
    endTime: str | None = None
    maxDepth: int | None = 1


class RegisterMonitoredItemsRequest(BaseModel):
    subscriptionId: str
    elementIds: list[str]
    maxDepth: int | None = 1


class SyncRequest(BaseModel):
    clientId: str | None = None
    subscriptionId: str
    acknowledgeSequence: int = Field(default=0, validation_alias=AliasChoices("acknowledgeSequence", "lastSequenceNumber"))


class ListSubscriptionsRequest(BaseModel):
    subscriptionIds: list[str] = Field(default_factory=list)


class DeleteSubscriptionsRequest(BaseModel):
    subscriptionIds: list[str]


class StreamRequest(BaseModel):
    subscriptionId: str
    acknowledgeSequence: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("acknowledgeSequence", "lastSequenceNumber"),
    )


class CreateSubscriptionRequest(BaseModel):
    clientId: str | None = None
    displayName: str | None = None


class CreateSubscriptionResponse(BaseModel):
    subscriptionId: str
    clientId: str | None = None
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
    nodeId: str
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
        query=QueryCapabilities(history=False),
        update=UpdateCapabilities(current=False, history=False),
        subscribe=SubscribeCapabilities(stream=True),
    )


def _build_server_info() -> ServerInfo:
    return ServerInfo(
        specVersion="beta",
        serverVersion="0.1.0",
        serverName="i3X OPC UA Provider",
        capabilities=_supported_capabilities(),
    )


def _namespace_infos_by_uri(namespace_infos: list[OpcUaNamespaceInfo]) -> dict[str, OpcUaNamespaceInfo]:
    return {item.uri: item for item in namespace_infos}


def _namespace_uri_for_node_id(node_id: str, namespace_infos: list[OpcUaNamespaceInfo]) -> str:
    match = re.search(r"ns=(\d+)", node_id)
    namespace_index = int(match.group(1)) if match is not None else 0
    if 0 <= namespace_index < len(namespace_infos):
        return namespace_infos[namespace_index].uri
    return ""


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


def _to_object_type(
    item: OpcUaObjectTypeInfo,
    namespace_infos: list[OpcUaNamespaceInfo],
    object_types_by_node_id: dict[str, OpcUaObjectTypeInfo],
    element_ids_by_node_id: dict[str, str],
) -> ObjectTypeResponse:
    namespace_uri = _namespace_uri_for_node_id(item.node_id, namespace_infos)
    element_id = _object_type_element_id(item, namespace_uri)
    source_type_id = item.parent_node_id or item.node_id
    return ObjectTypeResponse(
        elementId=element_id,
        displayName=item.display_name,
        namespaceUri=namespace_uri,
        sourceTypeId=source_type_id,
        schema=build_object_type_schema(item, object_types_by_node_id, element_ids_by_node_id),
        related=None,
    )


def _object_type_element_ids_by_node_id(
    object_types: list[OpcUaObjectTypeInfo],
    namespace_infos: list[OpcUaNamespaceInfo],
) -> dict[str, str]:
    return {
        item.node_id: _object_type_element_id(item, _namespace_uri_for_node_id(item.node_id, namespace_infos))
        for item in object_types
    }


def _relationship_type_items() -> list[RelationshipType]:
    return [
        RelationshipType(
            elementId="has-component",
            displayName="HasComponent",
            namespaceUri="http://opcfoundation.org/UA/",
            relationshipId="HasComponent",
            reverseOf="ComponentOf",
        ),
        RelationshipType(
            elementId="has-property",
            displayName="HasProperty",
            namespaceUri="http://opcfoundation.org/UA/",
            relationshipId="HasProperty",
            reverseOf="PropertyOf",
        ),
        RelationshipType(
            elementId="has-subtype",
            displayName="HasSubtype",
            namespaceUri="http://opcfoundation.org/UA/",
            relationshipId="HasSubtype",
            reverseOf="SubtypeOf",
        ),
        RelationshipType(
            elementId="has-type-definition",
            displayName="HasTypeDefinition",
            namespaceUri="http://opcfoundation.org/UA/",
            relationshipId="HasTypeDefinition",
            reverseOf="TypeDefinitionOf",
        ),
    ]


def _find_model_node(model: BuildResult, element_id: str) -> ModelNode | None:
    node = model.nodes_by_id.get(element_id)
    if node is not None:
        return node
    for candidate in model.nodes_by_id.values():
        if candidate.name == element_id:
            return candidate
        if candidate.type == element_id:
            return candidate
    return None


def _parent_id_for_node(model: BuildResult, node_id: str) -> str | None:
    for parent_id, child_ids in model.children_by_id.items():
        if node_id in child_ids:
            return parent_id
    return None


def _to_object_instance(model: BuildResult, node: ModelNode, include_metadata: bool) -> ObjectInstanceResponse:
    metadata = None
    if include_metadata:
        metadata = ObjectInstanceMetadata(
            sourceTypeId=node.source_node_id,
            description=f"Derived from model node {node.name}",
            relationships={"children": list(node.children)},
        )
    return ObjectInstanceResponse(
        elementId=node.id,
        displayName=node.name,
        typeElementId=node.type or node.kind,
        parentId=_parent_id_for_node(model, node.id),
        isComposition=bool(node.children),
        isExtended=False,
        metadata=metadata,
    )


def _relationship_type_for_child(child: ModelNode) -> RelationshipType:
    if child.kind == "property":
        return RelationshipType(
            elementId="has-property",
            displayName="HasProperty",
            namespaceUri="http://opcfoundation.org/UA/",
            relationshipId="HasProperty",
            reverseOf="PropertyOf",
        )
    return RelationshipType(
        elementId="has-component",
        displayName="HasComponent",
        namespaceUri="http://opcfoundation.org/UA/",
        relationshipId="HasComponent",
        reverseOf="ComponentOf",
    )


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
    namespace_uri: str | None = Query(default=None, alias="namespaceUri"),
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> SuccessResponse[list[ObjectTypeResponse]]:
    try:
        namespace_infos = await opcua_client.get_namespace_infos()
        object_types = await opcua_client.get_object_types()
    except Exception as exc:
        raise i3x_http_error(
            502,
            "OpcUaObjectTypesError",
            "Failed to read OPC UA object types",
            {"cause": str(exc)},
        ) from exc

    object_types_by_node_id = {item.node_id: item for item in object_types}
    element_ids_by_node_id = _object_type_element_ids_by_node_id(object_types, namespace_infos)
    items = [
        _to_object_type(item, namespace_infos, object_types_by_node_id, element_ids_by_node_id)
        for item in object_types
    ]
    if namespace_uri:
        items = [item for item in items if item.namespaceUri == namespace_uri]
    return SuccessResponse(result=items)


@router.post("/objecttypes/query", response_model=BulkResponse[ObjectTypeResponse])
async def query_object_types_v1(
    body: GetObjectTypesRequest,
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> BulkResponse[ObjectTypeResponse]:
    try:
        namespace_infos = await opcua_client.get_namespace_infos()
        object_types = await opcua_client.get_object_types()
    except Exception as exc:
        raise i3x_http_error(
            502,
            "OpcUaObjectTypesError",
            "Failed to query OPC UA object types",
            {"cause": str(exc)},
        ) from exc

    object_types_by_node_id = {item.node_id: item for item in object_types}
    element_ids_by_node_id = _object_type_element_ids_by_node_id(object_types, namespace_infos)
    indexed = {
        item.elementId: item
        for item in (
            _to_object_type(item, namespace_infos, object_types_by_node_id, element_ids_by_node_id)
            for item in object_types
        )
    }
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
            results.append(
                BulkResultItem[ObjectTypeResponse](success=True, elementId=element_id, result=match)
            )
    return BulkResponse(results=results)


@router.get("/relationshiptypes", response_model=SuccessResponse[list[RelationshipType]])
async def get_relationship_types(
    namespace_uri: str | None = Query(default=None, alias="namespaceUri"),
) -> SuccessResponse[list[RelationshipType]]:
    items = _relationship_type_items()
    if namespace_uri is not None:
        items = [item for item in items if item.namespaceUri == namespace_uri]
    return SuccessResponse(result=items)


@router.post("/relationshiptypes/query", response_model=BulkResponse[RelationshipType])
async def query_relationship_types(body: GetRelationshipTypesRequest) -> BulkResponse[RelationshipType]:
    items = {item.elementId: item for item in _relationship_type_items()}
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
            results.append(
                BulkResultItem[RelationshipType](success=True, elementId=element_id, result=match)
            )
    return BulkResponse(results=results)


@router.get("/objects", response_model=SuccessResponse[list[ObjectInstanceResponse]])
async def get_objects_v1(
    type_element_id: str | None = Query(default=None, alias="typeElementId"),
    include_metadata: bool = Query(default=False, alias="includeMetadata"),
    root: bool | None = Query(default=None),
    model: BuildResult = Depends(get_or_build_model),
) -> SuccessResponse[list[ObjectInstanceResponse]]:
    if root is True:
        nodes = [model.nodes_by_id[node_id] for node_id in model.root_ids if node_id in model.nodes_by_id]
    else:
        nodes = list(model.nodes_by_id.values())
    if type_element_id is not None:
        nodes = [node for node in nodes if node.type == type_element_id or node.kind == type_element_id]
    return SuccessResponse(result=[_to_object_instance(model, node, include_metadata) for node in nodes])


@router.post("/objects/list", response_model=BulkResponse[ObjectInstanceResponse])
async def list_objects_by_id_v1(
    body: GetObjectsRequest,
    model: BuildResult = Depends(get_or_build_model),
) -> BulkResponse[ObjectInstanceResponse]:
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
                result=_to_object_instance(model, node, include_metadata=False),
            )
        )
    return BulkResponse(results=results)


@router.post("/objects/related", response_model=BulkResponse[list[RelatedObjectResult]])
async def query_related_objects_v1(
    body: GetRelatedObjectsRequest,
    model: BuildResult = Depends(get_or_build_model),
) -> BulkResponse[list[RelatedObjectResult]]:
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
        for child_id in model.children_by_id.get(node.id, []):
            child = model.nodes_by_id.get(child_id)
            if child is None:
                continue
            relationship = _relationship_type_for_child(child)
            if body.relationshipType is not None and relationship.elementId != body.relationshipType:
                continue
            related.append(
                RelatedObjectResult(
                    sourceRelationship=relationship.displayName,
                    object=_to_object_instance(model, child, include_metadata=body.includeMetadata),
                )
            )
        results.append(
            BulkResultItem[list[RelatedObjectResult]](
                success=True,
                elementId=element_id,
                result=related,
            )
        )
    return BulkResponse(results=results)


@router.post("/objects/value", response_model=BulkResponse[CurrentValueResult])
async def query_last_known_values_v1(
    body: GetObjectValueRequest,
    model: BuildResult = Depends(get_or_build_model),
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> BulkResponse[CurrentValueResult]:
    node_ids: list[str] = []
    ordered_nodes: list[tuple[str, ModelNode]] = []
    for element_id in body.elementIds:
        node = _find_model_node(model, element_id)
        if node is None:
            continue
        node_ids.append(node.source_node_id)
        ordered_nodes.append((element_id, node))

    values_by_node_id: dict[str, Any] = {}
    if node_ids:
        try:
            raw_values = await opcua_client.read_values(node_ids)
            if isinstance(raw_values, dict):
                values_by_node_id = {str(key): value for key, value in raw_values.items()}
            else:
                values_by_node_id = {
                    node_id: value
                    for node_id, value in zip(node_ids, raw_values, strict=False)
                }
        except Exception as exc:
            raise i3x_http_error(
                502,
                "OpcUaReadError",
                "Failed to read OPC UA values",
                {"cause": str(exc)},
            ) from exc

    results: list[BulkResultItem[CurrentValueResult]] = []
    for element_id, node in ordered_nodes:
        value = values_by_node_id.get(node.source_node_id)
        if value is None:
            results.append(
                BulkResultItem[CurrentValueResult](
                    success=False,
                    elementId=element_id,
                    error=ErrorDetail(code=404, message="Object value not found"),
                )
            )
            continue
        results.append(
            BulkResultItem[CurrentValueResult](
                success=True,
                elementId=element_id,
                result=CurrentValueResult(
                    elementId=element_id,
                    value=VQT(value=value, quality="Good", timestamp=datetime.now(UTC).isoformat()),
                ),
            )
        )
    return BulkResponse(results=results)


@router.post("/objects/history")
async def query_historical_values_v1() -> None:
    _not_implemented("Historical value queries")


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
            clientId=created.client_id,
            displayName=created.display_name,
        )
    )


@router.post("/subscriptions/register")
async def register_monitored_items_v1(
    body: RegisterMonitoredItemsRequest,
    model: BuildResult = Depends(get_or_build_model),
    subscription_service: SubscriptionService = Depends(get_subscription_service),
) -> SuccessResponse[None]:
    max_depth = body.maxDepth or 1
    ok = await subscription_service.register_items(
        subscription_id=body.subscriptionId,
        element_ids=body.elementIds,
        max_depth=max_depth,
        model=model,
    )
    if not ok:
        raise i3x_http_error(
            404,
            "SubscriptionNotFound",
            f"Subscription '{body.subscriptionId}' not found",
        )
    return SuccessResponse(result=None)


@router.post("/subscriptions/unregister")
async def remove_monitored_items_v1(
    body: RegisterMonitoredItemsRequest,
    model: BuildResult = Depends(get_or_build_model),
    subscription_service: SubscriptionService = Depends(get_subscription_service),
) -> SuccessResponse[None]:
    ok = await subscription_service.unregister_items(
        subscription_id=body.subscriptionId,
        element_ids=body.elementIds,
        model=model,
    )
    if not ok:
        raise i3x_http_error(
            404,
            "SubscriptionNotFound",
            f"Subscription '{body.subscriptionId}' not found",
        )
    return SuccessResponse(result=None)


@router.post("/subscriptions/stream")
async def stream_subscription_v1(
    body: StreamRequest,
    subscription_service: SubscriptionService = Depends(get_subscription_service),
) -> StreamingResponse:
    acknowledged = await subscription_service.sync(
        subscription_id=body.subscriptionId,
        acknowledge_sequence=body.acknowledgeSequence,
    )
    if acknowledged is None:
        raise i3x_http_error(
            404,
            "SubscriptionNotFound",
            f"Subscription '{body.subscriptionId}' not found",
        )

    async def event_stream() -> Any:
        last_sequence = body.acknowledgeSequence
        while True:
            updates = await subscription_service.wait_for_updates(
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
                    "elementId": item.element_id,
                    "nodeId": item.node_id,
                    "value": item.value,
                    "quality": item.quality,
                    "timestamp": item.timestamp,
                }
                for item in updates
            ]
            encoded_payload = jsonable_encoder(payload)
            yield f"data: {json.dumps(encoded_payload)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.post("/subscriptions/sync")
async def sync_subscription_v1(
    body: SyncRequest,
    subscription_service: SubscriptionService = Depends(get_subscription_service),
) -> SuccessResponse[list[SyncUpdate]]:
    synced = await subscription_service.sync(
        subscription_id=body.subscriptionId,
        acknowledge_sequence=body.acknowledgeSequence,
    )
    if synced is None:
        raise i3x_http_error(
            404,
            "SubscriptionNotFound",
            f"Subscription '{body.subscriptionId}' not found",
        )
    return SuccessResponse(
        result=[
            SyncUpdate(
                sequenceNumber=item.sequence_number,
                elementId=item.element_id,
                nodeId=item.node_id,
                value=item.value,
                quality=item.quality,
                timestamp=item.timestamp,
            )
            for item in synced.updates
        ]
    )


@router.post("/subscriptions/delete")
async def delete_subscriptions_v1(
    body: DeleteSubscriptionsRequest,
    subscription_service: SubscriptionService = Depends(get_subscription_service),
) -> BulkResponse[None]:
    deleted = await subscription_service.delete_subscriptions(body.subscriptionIds)
    return BulkResponse(
        results=[
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
    subscription_service: SubscriptionService = Depends(get_subscription_service),
) -> SuccessResponse[list[SubscriptionDetail]]:
    filter_ids = body.subscriptionIds or None
    subscriptions = await subscription_service.list_subscriptions(filter_ids)
    return SuccessResponse(
        result=[
            SubscriptionDetail(
                subscriptionId=item.subscription_id,
                clientId=item.client_id,
                displayName=item.display_name,
                monitoredObjects=item.monitored_objects,
                mode=item.mode,
            )
            for item in subscriptions
        ]
    )
