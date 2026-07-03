from __future__ import annotations

import http
from typing import Any, Generic, TypeVar

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

T = TypeVar("T")


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


def _status_title(status_code: int) -> str:
    try:
        return http.HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


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


def _map_lookup_bulk_result_items(
    element_ids: list[str],
    indexed_items: dict[str, T],
    *,
    not_found_message: str,
) -> list[BulkResultItem[T]]:
    results: list[BulkResultItem[T]] = []
    for element_id in element_ids:
        match = indexed_items.get(element_id)
        if match is None:
            results.append(
                BulkResultItem[T](
                    success=False,
                    elementId=element_id,
                    error=ErrorDetail(code=404, message=not_found_message),
                )
            )
            continue
        results.append(BulkResultItem[T](success=True, elementId=element_id, result=match))
    return results


def _bulk_result_success(element_id: str, result: T) -> BulkResultItem[T]:
    return BulkResultItem[T](
        success=True,
        elementId=element_id,
        result=result,
    )


def _bulk_result_error(element_id: str, message: str, code: int = 404) -> BulkResultItem[T]:
    return BulkResultItem[T](
        success=False,
        elementId=element_id,
        error=ErrorDetail(code=code, message=message),
    )


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


class ModelBuildMetrics(BaseModel):
    browseDurationS: float
    mapDurationS: float
    totalDurationS: float
    buildCompletedAtUtc: str | None = None


class ModelVolumeMetrics(BaseModel):
    totalNodes: int
    rootNodes: int
    byKind: dict[str, int] = Field(default_factory=dict)


class ModelRelationshipMetrics(BaseModel):
    hierarchyEdges: int
    compositionEdges: int
    graphEdges: int
    uniqueGraphRelationshipNames: int
    byRelationshipName: dict[str, int] = Field(default_factory=dict)


class ModelQualityMetrics(BaseModel):
    confidence: dict[str, int] = Field(default_factory=dict)
    semanticRole: dict[str, int] = Field(default_factory=dict)
    lowConfidenceNodes: int
    unknownSemanticRoleNodes: int


class ModelCoverageMetrics(BaseModel):
    readableProperties: int
    invokableActions: int
    typedInstanceGroups: int
    typedInstances: int


class ModelContextMetrics(BaseModel):
    namespaceCounts: dict[str, int] = Field(default_factory=dict)
    nodesWithoutNamespace: int
    appliedProfileCounts: dict[str, int] = Field(default_factory=dict)
    nodesWithoutProfiles: int


class ModelMetricsResponse(BaseModel):
    build: ModelBuildMetrics
    volume: ModelVolumeMetrics
    relationships: ModelRelationshipMetrics
    quality: ModelQualityMetrics
    coverage: ModelCoverageMetrics
    context: ModelContextMetrics


class ModelNamespaceGapItem(BaseModel):
    elementId: str
    displayName: str
    kind: str
    sourceNodeId: str
    sourceNamespaceIndex: int | None = None


class ModelNamespaceGapResponse(BaseModel):
    totalMissing: int
    byKind: dict[str, int] = Field(default_factory=dict)
    byNamespaceIndex: dict[str, int] = Field(default_factory=dict)
    items: list[ModelNamespaceGapItem] = Field(default_factory=list)


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
    components: dict[str, HistoricalValueResult] | None = None


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


class UpdateObjectValueRequest(BaseModel):
    value: Any | None = None
    maxDepth: int | None = Field(
        default=1,
        ge=0,
        description=(
            "Maximum composition depth to apply when updating descendant properties. "
            "Use 0 for unlimited composition depth."
        ),
    )


class WriteVQTRequest(BaseModel):
    value: Any
    quality: str | None = None
    timestamp: str | None = None


class ValueUpdateItemRequest(BaseModel):
    elementId: str
    value: Any


class UpdateObjectValuesRequest(BaseModel):
    updates: list[ValueUpdateItemRequest]


class RegisterMonitoredItemsRequest(BaseModel):
    clientId: str | None = None
    subscriptionId: str
    elementIds: list[str]
    maxDepth: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Maximum composition depth to monitor beneath each registered object. "
            "Omit or pass null to monitor all descendant properties."
        ),
    )


class SyncRequest(BaseModel):
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


class SyncBatch(BaseModel):
    sequenceNumber: int
    updates: list[SyncUpdate] = Field(default_factory=list)
