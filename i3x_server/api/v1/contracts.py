from __future__ import annotations

import http
import re
from datetime import datetime, timezone
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import (
    AfterValidator,
    AliasChoices,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

T = TypeVar("T")


def validate_write_element_id(value: str) -> str:
    if not value or value != value.strip() or not value.isprintable():
        raise ValueError("elementId must be non-empty, printable, and have no surrounding whitespace")
    return value


WriteElementId = Annotated[str, AfterValidator(validate_write_element_id)]


def validate_client_id(value: str) -> str:
    """Normalize the subscription scope identifier.

    Every subscription endpoint must treat ``"app"`` and ``" app "`` as the same scope: the service stores
    and compares ``clientId`` verbatim, so an untrimmed value would make ``/subscriptions/list`` and
    ``/subscriptions/delete`` fail to find a subscription that ``/subscriptions`` created.
    """
    normalized = value.strip()
    if not normalized:
        raise ValueError("clientId must be non-empty and free of surrounding whitespace")
    if not normalized.isprintable():
        raise ValueError("clientId must be printable")
    return normalized


ClientId = Annotated[str, AfterValidator(validate_client_id)]

# Bulk arrays are capped: an unbounded ``elementIds``/``updates`` list turns one request into
# thousands of upstream OPC UA round-trips. 500 matches the documented MCP batch limit
# (``MCP_ID_RESOLUTION_MAX_ITEMS``), so REST is not weaker than the MCP surface. Overflow is a
# documented 400 via the ``RequestValidationError`` handler, not a slow partial read.
MAX_BULK_ITEMS = 500


class SuccessResponse(BaseModel, Generic[T]):
    success: bool
    result: T


class ErrorDetail(BaseModel):
    code: int
    message: str


class ResponseDetail(BaseModel):
    title: str
    status: int
    detail: str


class HttpErrorResponse(BaseModel):
    success: Literal[False]
    error: ErrorDetail
    responseDetail: ResponseDetail


class BulkResultItem(BaseModel, Generic[T]):
    success: bool
    elementId: str | None = None
    subscriptionId: str | None = None
    result: T | None = None
    error: ErrorDetail | None = None
    responseDetail: ResponseDetail | None = None


class BulkResponse(BaseModel, Generic[T]):
    success: bool
    results: list[BulkResultItem[T]]
    # Top-level detail for partial results: HTTP 206 when server limits truncated a composition
    # tree (i3X spec IMPLEMENTATION_GUIDE.md:1113-1119, conformance query.js QRY-05 requires it).
    responseDetail: ResponseDetail | None = None


def _status_title(status_code: int) -> str:
    try:
        return http.HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


def _bulk_response(
    results: list[BulkResultItem[T]],
    response_detail: ResponseDetail | None = None,
) -> BulkResponse[T]:
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
    return BulkResponse(success=all(item.success for item in results), results=results, responseDetail=response_detail)


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
    schema_: dict[str, Any] = Field(alias="schema")
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
    parentId: str | None
    isComposition: bool
    isExtended: bool
    metadata: ObjectInstanceMetadata | None = None


class RelationshipType(BaseModel):
    elementId: str
    displayName: str
    namespaceUri: str
    relationshipId: str
    reverseOf: str


class VQT(BaseModel):
    value: Any = Field(description="Source value; may be null when no valid data is available.")
    quality: str = Field(description="Source quality: Good, Uncertain, Bad, or GoodNoData.")
    timestamp: str = Field(description="Source timestamp encoded as an RFC 3339 date-time.")


class CurrentValueResult(BaseModel):
    isComposition: bool
    value: Any
    quality: str
    timestamp: str
    components: dict[str, VQT] | None = None


class HistoricalValueResult(BaseModel):
    isComposition: bool
    values: list[VQT]
    components: dict[str, HistoricalValueResult] | None = None


class RelatedObjectResult(BaseModel):
    sourceRelationship: str
    object: ObjectInstanceResponse


class GetObjectTypesRequest(BaseModel):
    elementIds: list[str] = Field(min_length=1, max_length=MAX_BULK_ITEMS)


class GetRelationshipTypesRequest(BaseModel):
    elementIds: list[str] = Field(min_length=1, max_length=MAX_BULK_ITEMS)


class GetObjectsRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"elementIds": ["property-0123456789abcdef"], "includeMetadata": True}]}
    )

    elementIds: list[str] = Field(min_length=1, max_length=MAX_BULK_ITEMS)
    includeMetadata: bool = False


class GetRelatedObjectsRequest(BaseModel):
    elementIds: list[str] = Field(min_length=1, max_length=MAX_BULK_ITEMS)
    relationshipType: str | None = None
    includeMetadata: bool = False


class GetObjectValueRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"elementIds": ["property-0123456789abcdef"], "maxDepth": 1}]}
    )

    elementIds: list[str] = Field(
        min_length=1,
        max_length=MAX_BULK_ITEMS,
        description="Object element IDs returned by discovery; examples are placeholders.",
    )
    maxDepth: int = Field(
        default=1,
        ge=0,
        description="Composition depth in levels: 1 = no recursion, only this element's own value "
        "(default); N > 1 = recurse up to N levels through composition (`HasComponent`) children; "
        "0 = unlimited recursion, bounded by the server's composition limits "
        "(`I3X_COMPOSITION_MAX_DEPTH`, `I3X_COMPOSITION_MAX_NODES`); a tree truncated by those limits "
        "is answered as HTTP 206 with a top-level `responseDetail`. Hierarchy-only children "
        "(`HasChildren`) are never traversed - query them as separate elements.",
    )


class GetObjectHistoryRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "elementIds": ["property-0123456789abcdef"],
                    "startTime": "2026-10-14T06:00:00Z",
                    "endTime": "2026-10-14T07:00:00Z",
                    "maxDepth": 1,
                }
            ]
        }
    )

    elementIds: list[str] = Field(min_length=1, max_length=MAX_BULK_ITEMS)
    startTime: str = Field(description="Start of the history interval; RFC 3339 date-time with timezone.")
    endTime: str = Field(description="End of the history interval; RFC 3339 date-time with timezone.")
    maxDepth: int = Field(
        default=1,
        ge=0,
        description="Composition depth in levels: 1 = no recursion, only this element's own history "
        "(default); N > 1 = recurse up to N levels through composition (`HasComponent`) children; "
        "0 = unlimited recursion, bounded by the server's composition limits "
        "(`I3X_COMPOSITION_MAX_DEPTH`, `I3X_COMPOSITION_MAX_NODES`); a tree truncated by those limits "
        "is answered as HTTP 206 with a top-level `responseDetail`. Hierarchy-only children "
        "(`HasChildren`) are never traversed - query them as separate elements.",
    )

    @field_validator("startTime", "endTime")
    @classmethod
    def _require_rfc3339_timestamp(cls, value: str) -> str:
        if (
            re.fullmatch(
                r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})",
                value,
            )
            is None
        ):
            raise ValueError("timestamp must be an RFC 3339 date-time with a timezone")
        try:
            datetime.fromisoformat(value[:-1] + "+00:00" if value[-1:] in {"Z", "z"} else value)
        except ValueError as exc:
            raise ValueError("timestamp must be a valid RFC 3339 date-time") from exc
        return value


class WriteVQTRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any = Field(
        description="Value matching the discovered object's schema. Null requires Bad or GoodNoData quality."
    )
    quality: Literal["Good", "GoodNoData", "Bad", "Uncertain"] = Field(
        default="Good", description="Bad and GoodNoData require a null value."
    )
    timestamp: AwareDatetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description=(
            "RFC 3339 timestamp with a timezone (Z or numeric offset). Omission uses the current server UTC time."
        ),
    )

    @field_validator("timestamp", mode="before")
    @classmethod
    def _require_rfc3339_timestamp(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("timestamp must be an RFC 3339 string")
        timestamp: str = value
        if (
            re.fullmatch(
                r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})",
                timestamp,
            )
            is None
        ):
            raise ValueError("timestamp must be an RFC 3339 date-time with a timezone")
        if timestamp.endswith("z"):
            timestamp = timestamp[:-1] + "Z"
        return timestamp

    @model_validator(mode="after")
    def _validate_null_quality(self) -> WriteVQTRequest:
        if self.value is None and self.quality not in {"Bad", "GoodNoData"}:
            raise ValueError("A null value requires Bad or GoodNoData quality")
        if self.quality in {"Bad", "GoodNoData"} and self.value is not None:
            raise ValueError("Bad and GoodNoData quality require a null value")
        return self


class ValueUpdateItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    elementId: WriteElementId
    value: WriteVQTRequest


class UpdateObjectValuesRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "updates": [
                        {
                            "elementId": "property-0123456789abcdef",
                            "value": {
                                "value": 23.5,
                                "quality": "Good",
                                "timestamp": "2026-10-14T06:00:00Z",
                            },
                        }
                    ]
                }
            ]
        },
    )

    updates: list[ValueUpdateItemRequest] = Field(min_length=1, max_length=MAX_BULK_ITEMS)


class HistoryWriteVQTRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any = Field(
        description="Value matching the discovered object's schema. Null requires Bad or GoodNoData quality."
    )
    quality: Literal["Good", "Uncertain", "Bad", "GoodNoData"] = Field(
        description="Required historical quality. Bad and GoodNoData require a null value."
    )
    timestamp: AwareDatetime = Field(
        description="Required RFC 3339 timestamp with a timezone, exactly representable at microsecond precision."
    )

    @field_validator("timestamp", mode="before")
    @classmethod
    def _require_rfc3339_timestamp(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("timestamp must be a UTC RFC 3339 string")
        normalized = value
        if (
            re.fullmatch(
                r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})",
                normalized,
            )
            is None
        ):
            raise ValueError("timestamp must be an RFC 3339 date-time with a timezone")
        fractional = re.search(r"\.(\d+)", normalized)
        fractional_digits = fractional.group(1) if fractional is not None else ""
        if any(digit != "0" for digit in fractional_digits[6:]):
            raise ValueError("timestamp cannot be represented exactly at microsecond precision")
        if len(fractional_digits) > 6:
            normalized = normalized.replace(f".{fractional_digits}", f".{fractional_digits[:6]}", 1)
        if normalized[-1:] in {"Z", "z"}:
            normalized = normalized[:-1] + "+00:00"
        try:
            datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("timestamp must be a valid RFC 3339 date-time") from exc
        return normalized

    @model_validator(mode="after")
    def _validate_null_quality(self) -> HistoryWriteVQTRequest:
        if self.value is None and self.quality not in {"Bad", "GoodNoData"}:
            raise ValueError("A null value requires Bad or GoodNoData quality")
        if self.quality in {"Bad", "GoodNoData"} and self.value is not None:
            raise ValueError("Bad and GoodNoData quality require a null value")
        return self


class HistoryUpdateItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    elementId: WriteElementId
    value: HistoryWriteVQTRequest


class UpdateObjectHistoryRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "updates": [
                        {
                            "elementId": "property-0123456789abcdef",
                            "value": {
                                "value": 23.5,
                                "quality": "Good",
                                "timestamp": "2026-10-14T06:00:00Z",
                            },
                        }
                    ]
                }
            ]
        },
    )

    updates: list[HistoryUpdateItemRequest] = Field(min_length=1, max_length=MAX_BULK_ITEMS)


class RegisterMonitoredItemsRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "clientId": "example-client",
                    "subscriptionId": "subscription-from-create",
                    "elementIds": ["property-0123456789abcdef"],
                    "maxDepth": 1,
                }
            ]
        }
    )

    clientId: ClientId
    subscriptionId: str
    elementIds: list[str] = Field(min_length=1, max_length=MAX_BULK_ITEMS)
    maxDepth: int = Field(
        default=1,
        ge=0,
        description=(
            "Maximum composition depth to monitor beneath each registered object. "
            "Use 0 to monitor all descendant properties."
        ),
    )


class UnregisterMonitoredItemsRequest(BaseModel):
    clientId: ClientId
    subscriptionId: str
    elementIds: list[str] = Field(min_length=1, max_length=MAX_BULK_ITEMS)


class SyncRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "clientId": "example-client",
                    "subscriptionId": "subscription-from-create",
                    "lastSequenceNumber": 42,
                }
            ]
        }
    )

    clientId: ClientId
    subscriptionId: str
    lastSequenceNumber: int | None = Field(
        default=None,
        description=(
            "Last sequence number the client has processed. All updates with a sequence number "
            "<= this value are removed from the pending queue. "
            "Pass -1 to acknowledge and discard all pending updates. "
            "Omit or pass null on the first call to receive all buffered updates without discarding any."
        ),
        validation_alias=AliasChoices("lastSequenceNumber", "acknowledgeSequence"),
    )


class ListSubscriptionsRequest(BaseModel):
    clientId: ClientId
    subscriptionIds: list[str] = Field(min_length=1, max_length=MAX_BULK_ITEMS)


class DeleteSubscriptionsRequest(BaseModel):
    clientId: ClientId
    subscriptionIds: list[str] = Field(min_length=1, max_length=MAX_BULK_ITEMS)


class StreamRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "clientId": "example-client",
                    "subscriptionId": "subscription-from-create",
                }
            ]
        },
    )

    clientId: ClientId
    subscriptionId: str


class CreateSubscriptionRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"clientId": "example-client", "displayName": "Machine telemetry"}]}
    )

    clientId: ClientId = Field(description="Unique client scope identifier. This is not an authentication credential.")
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
    updates: list[SyncUpdate]


class PartialSyncResponse(BaseModel):
    success: Literal[True]
    result: list[SyncBatch]
    responseDetail: ResponseDetail


class HistoryWriteUnavailableResponse(BulkResponse[None]):
    success: Literal[False]
    responseDetail: ResponseDetail
