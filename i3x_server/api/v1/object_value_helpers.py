"""Presentation-local helpers for the v1 object value endpoints.

ADR 0004 rule 4: HTTP-adjacent behavior that is not business-facing workflow
orchestration lives in focused presentation-local helper modules. The value
write path depends on ``write_validation`` (presentation), so it cannot move to
an application service without inverting the layering contract, and the bulk
endpoints own per-item diagnostics and HTTP 206 truncation semantics that the
application ``ObjectValueService`` does not implement (see the decision update
in ``docs/adr/0004-v1-api-ownership-model.md``).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections import deque
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, cast

from asyncua.ua.status_codes import StatusCodes

from i3x_server.api.v1.common_helpers import (
    _format_utc_timestamp,
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
    ValueUpdateItemRequest,
    validate_write_element_id,
)
from i3x_server.api.v1.object_helpers import _composition_children_for_node, _find_model_node
from i3x_server.api.v1.write_validation import WriteValidationError, WriteValueValidator
from i3x_server.application.ports.opcua import OpcUaClientProtocol
from i3x_server.config.settings import Settings, get_settings
from i3x_server.domain.ports.opcua import (
    OpcUaWriteMetadataUnsupportedError,
    OpcUaWriteMetadataVerificationError,
)
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult


def _runtime_settings() -> Settings:
    return get_settings()


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


def _decode_bytestring_write_value(value: Any, variant_type: str | None) -> Any:
    if variant_type is None or variant_type.strip().lower() != "bytestring" or not isinstance(value, str):
        return value
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Invalid base64 ByteString") from exc


def _write_status_code(exc: BaseException) -> int | None:
    """Raw OPC UA status code carried by an exception, ``None`` when it carries none."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    value = getattr(code, "value", None)
    return value if isinstance(value, int) else None


# Stable OPC UA status codes for write failures. Classification is code-based: exception text
# depends on the asyncua version and its language, and matching on it reported an upstream-deleted
# node or a rejected write as retryable gateway trouble (502 ``session_or_transport_failure``).
# Codes describing the request or the target must not be reported as gateway failures, because
# clients retry 502 and would retry a permanently wrong request.
_WRITE_STATUS_CLASSIFICATION: dict[int, tuple[int, str]] = {
    StatusCodes.BadNodeIdUnknown: (404, "node_not_found"),
    StatusCodes.BadUserAccessDenied: (403, "unauthorized_by_opcua_server"),
    StatusCodes.BadNotWritable: (403, "target_not_writable"),
    StatusCodes.BadTypeMismatch: (400, "bad_type_or_range"),
    StatusCodes.BadOutOfRange: (400, "bad_type_or_range"),
    StatusCodes.BadInvalidArgument: (400, "bad_type_or_range"),
    StatusCodes.BadSessionClosed: (502, "session_or_transport_failure"),
    StatusCodes.BadSessionIdInvalid: (502, "session_or_transport_failure"),
    StatusCodes.BadSecureChannelClosed: (502, "session_or_transport_failure"),
    StatusCodes.BadConnectionClosed: (502, "session_or_transport_failure"),
    StatusCodes.BadServerHalted: (502, "session_or_transport_failure"),
}

# An unsupported *service* is reported as "this write is not available" (501) only when the failure
# refers to the write itself, i.e. current values. For historical writes the route decides per
# attempt whether the HistoryUpdate service is genuinely unavailable, so an unsupported access-read
# stays an upstream failure (502) instead of claiming service-wide unavailability.
_WRITE_UNSUPPORTED_STATUS_CODES = frozenset({StatusCodes.BadServiceUnsupported, StatusCodes.BadNotImplemented})

# Fallback for exceptions without a status code (asyncua socket/transport failures). ``TimeoutError``
# is a subclass of ``OSError`` since Python 3.11, so asyncio timeouts are covered by the isinstance
# check; the markers catch error types that expose neither a code nor an OSError.
_TRANSPORT_FAILURE_MARKERS: tuple[str, ...] = (
    "connection is closed",
    "connection is not open",
    "connection reset",
    "broken pipe",
    "transport closed",
    "timed out",
    "timeout",
)


def _classify_write_error(exc: Exception, *, service_unsupported_is_write_unavailable: bool = False) -> tuple[int, str]:
    """Map an upstream write failure to an HTTP status and error class.

    ``service_unsupported_is_write_unavailable`` is set by the current-value path, where a
    ``BadServiceUnsupported``/``BadNotImplemented`` rejection means the write itself is not offered.
    """
    status_code = _write_status_code(exc)
    if status_code is not None:
        if status_code in _WRITE_UNSUPPORTED_STATUS_CODES:
            if service_unsupported_is_write_unavailable:
                return 501, "write_not_supported_by_opcua_server"
            return 502, "opcua_write_failed"
        mapped = _WRITE_STATUS_CLASSIFICATION.get(status_code)
        if mapped is not None:
            return mapped
        # Any other rejection is an OPC UA server failure, not a gateway transport problem.
        return 502, "opcua_write_failed"
    if isinstance(exc, OSError):
        return 502, "session_or_transport_failure"
    if any(marker in str(exc).lower() for marker in _TRANSPORT_FAILURE_MARKERS):
        return 502, "session_or_transport_failure"
    return 502, "opcua_write_failed"


def _normalize_write_payload(payload: Any) -> Any:
    if isinstance(payload, Mapping):
        keys = set(payload.keys())
        if "value" in keys and keys <= {"value", "quality", "timestamp"}:
            return payload["value"]
    return payload


def _json_equivalent(left: Any, right: Any) -> bool:
    safe_left = _to_json_safe_value(left)
    safe_right = _to_json_safe_value(right)
    return json.dumps(safe_left, sort_keys=True) == json.dumps(safe_right, sort_keys=True)


def _noop_write_matches(
    data_value: Any, requested_value: Any, requested_quality: str, requested_timestamp: datetime
) -> bool:
    """Compare a request against a read data value; a failed/empty read is never a no-op."""
    if data_value is None:
        return False
    current_vqt = _vqt_from_data_value(data_value)
    return (
        _json_equivalent(current_vqt.value, requested_value)
        and current_vqt.quality == requested_quality
        and getattr(data_value, "SourceTimestamp", None) == requested_timestamp.astimezone(timezone.utc)
    )


async def _write_object_values_bulk(
    *,
    model: BuildResult,
    opcua_client: OpcUaClientProtocol,
    updates: Sequence[ValueUpdateItemRequest],
    schema_validator: WriteValueValidator,
) -> list[tuple[str, bool, int, str]]:
    """Bulk current-value writes with prefetched access/variant reads.

    Per-item semantics are preserved (order, error classification, no-op fallback), but the
    per-node attribute round-trips are hoisted out of the loop: write access for every unique
    target node is gathered concurrently, current values for non-writable targets are read in
    one batch, and variant types are gathered for writable targets. Writes themselves stay
    sequential to keep request order and per-item attribution.
    """
    outcomes: list[tuple[str, bool, int, str] | None] = [None] * len(updates)
    pending: list[tuple[int, str, ModelNode, Any, str, datetime]] = []
    for index, update in enumerate(updates):
        element_id = update.elementId
        write_value = _normalize_write_payload(update.value.value)
        try:
            validate_write_element_id(element_id)
        except ValueError as exc:
            outcomes[index] = (element_id, False, 400, str(exc))
            continue
        node = _find_model_node(model, element_id)
        if node is None:
            outcomes[index] = (element_id, False, 404, f"Element not found: {element_id}")
            continue
        if node.kind != "property":
            outcomes[index] = (element_id, False, 400, "bad_type_or_range")
            continue
        try:
            await schema_validator.validate(node, write_value)
        except WriteValidationError as exc:
            outcomes[index] = (element_id, False, exc.status_code, str(exc))
            continue
        pending.append((index, element_id, node, write_value, update.value.quality, update.value.timestamp))

    if not pending:
        return [outcome for outcome in outcomes if outcome is not None]

    target_ids = list(
        dict.fromkeys(node.source_node_id for _index, _element_id, node, _value, _quality, _timestamp in pending)
    )
    access_results = await asyncio.gather(
        *(opcua_client.read_write_access(target_id) for target_id in target_ids),
        return_exceptions=True,
    )
    access_by_id: dict[str, Any] = dict(zip(target_ids, access_results, strict=False))

    noop_data: dict[str, Any] = {}
    noop_ids = [
        target_id
        for target_id in target_ids
        if isinstance(access_by_id[target_id], tuple)
        and not (access_by_id[target_id][0] and access_by_id[target_id][1])
    ]
    if noop_ids:
        try:
            current_values = await opcua_client.read_data_values(noop_ids)
            noop_data = dict(zip(noop_ids, current_values, strict=False))
        except Exception:
            noop_data = {}

    writable_ids = [target_id for target_id in target_ids if isinstance(access_by_id[target_id], tuple)]
    variant_results = await asyncio.gather(
        *(opcua_client.read_variant_type(target_id) for target_id in writable_ids),
        return_exceptions=True,
    )
    variant_by_id: dict[str, Any] = dict(zip(writable_ids, variant_results, strict=False))

    for index, element_id, node, write_value, quality, timestamp in pending:
        target_node_id = node.source_node_id
        access = access_by_id[target_node_id]
        if isinstance(access, BaseException):
            if not isinstance(access, Exception):
                raise access
            status_code, error_class = _classify_write_error(access, service_unsupported_is_write_unavailable=True)
            outcomes[index] = (element_id, False, status_code, error_class)
            continue
        writable, user_writable = access
        if not writable or not user_writable:
            if _noop_write_matches(noop_data.get(target_node_id), write_value, quality, timestamp):
                outcomes[index] = (element_id, True, 200, "ok")
                continue
            outcomes[index] = (element_id, False, 403, "target_not_writable")
            continue

        variant_type = variant_by_id.get(target_node_id)
        if isinstance(variant_type, BaseException):
            if not isinstance(variant_type, Exception):
                raise variant_type
            status_code, error_class = _classify_write_error(
                variant_type, service_unsupported_is_write_unavailable=True
            )
            outcomes[index] = (element_id, False, status_code, error_class)
            continue

        try:
            write_value = _decode_bytestring_write_value(write_value, variant_type)
        except ValueError:
            outcomes[index] = (element_id, False, 400, "bad_type_or_range")
            continue
        if write_value is not None and not _is_valid_write_type(write_value, variant_type):
            outcomes[index] = (element_id, False, 400, "bad_type_or_range")
            continue

        try:
            await opcua_client.write_value(
                target_node_id,
                write_value,
                variant_type=variant_type,
                quality=quality,
                timestamp=timestamp,
            )
        except OpcUaWriteMetadataUnsupportedError as exc:
            outcomes[index] = (element_id, False, 501, str(exc))
            continue
        except OpcUaWriteMetadataVerificationError as exc:
            outcomes[index] = (element_id, False, 502, str(exc))
            continue
        except Exception as exc:
            status_code, error_class = _classify_write_error(exc, service_unsupported_is_write_unavailable=True)
            outcomes[index] = (element_id, False, status_code, error_class)
            continue

        outcomes[index] = (element_id, True, 200, "ok")

    resolved: list[tuple[str, bool, int, str]] = [outcome for outcome in outcomes if outcome is not None]
    return resolved


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
    max_levels: int | None = None,
    max_nodes: int | None = None,
) -> tuple[list[tuple[str, ModelNode, list[ModelNode], list[ModelNode]]], list[str], bool]:
    """Collect history source nodes and node IDs for OPC UA read.

    Mirrors the value traversal (composition levels, spec :1105-1110) and reports truncation caused
    by the server's composition limits (spec :1113-1119), which the route answers with HTTP 206.
    ``max_nodes`` is a per-request budget: callers shrink it as elements consume the allowance.
    """
    lookup: list[tuple[str, ModelNode, list[ModelNode], list[ModelNode]]] = []
    node_ids: list[str] = []
    truncated = False
    collected = 0
    for element_id, node in resolved_nodes:
        if node is None:
            continue
        root_source_nodes = [node] if node.kind == "property" else []
        budget = None if max_nodes is None else max(0, max_nodes - collected)
        component_nodes, component_truncated = _collect_value_component_nodes(
            model, node, max_depth, max_levels=max_levels, max_nodes=budget
        )
        truncated = truncated or component_truncated
        collected += len(component_nodes) + len(root_source_nodes)
        lookup.append((element_id, node, root_source_nodes, component_nodes))
        for source_node in root_source_nodes:
            node_ids.append(source_node.source_node_id)
        for source_node in component_nodes:
            node_ids.append(source_node.source_node_id)
    return lookup, node_ids, truncated


def _build_historical_value_result(
    model: BuildResult,
    node: ModelNode,
    root_source_nodes: list[ModelNode],
    component_nodes: list[ModelNode],
    values_by_node_id: dict[str, list[Any]],
    end_time: datetime | None = None,
) -> HistoricalValueResult:
    """Build HistoricalValueResult from collected source nodes and values.

    A range without any stored samples is answered with a single `GoodNoData` record instead of an
    item-level error (i3X spec IMPLEMENTATION_GUIDE.md:1294 - "return `GoodNoData` for the requested
    range"; null values must carry `Bad`/`GoodNoData`, `:1163-1168`).
    """
    values: list[VQT] = []
    for source_node in root_source_nodes:
        raw_values = values_by_node_id.get(source_node.source_node_id, [])
        values.extend(_to_vqt_from_history_value(item) for item in raw_values)
    if not values:
        values.append(_history_no_data_vqt(end_time))
    values.sort(key=lambda item: item.timestamp)

    components: dict[str, HistoricalValueResult] = {}
    for component_node in component_nodes:
        raw_values = values_by_node_id.get(component_node.source_node_id, [])
        component_values = [_to_vqt_from_history_value(item) for item in raw_values]
        if not component_values:
            component_values.append(_history_no_data_vqt(end_time))
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


def _history_no_data_vqt(end_time: datetime | None) -> VQT:
    """Sample for a history interval the platform has no data for.

    The timestamp is the end of the requested range when that lies in the past, otherwise the
    current time - the record must sit inside the interval the client asked for.
    """
    now = datetime.now(timezone.utc)
    timestamp = min(now, end_time) if end_time is not None else now
    return VQT(value=None, quality="GoodNoData", timestamp=_format_utc_timestamp(timestamp))


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


def _composition_traversal_limits() -> tuple[int | None, int | None]:
    """Server-imposed bounds for composition recursion (`I3X_COMPOSITION_MAX_*`, spec :1107/:1113).

    ``None`` means unbounded; the configured ``0`` maps to unbounded as documented in the contract.
    """
    settings = _runtime_settings()
    max_levels = settings.composition_max_depth if settings.composition_max_depth > 0 else None
    max_nodes = settings.composition_max_nodes if settings.composition_max_nodes > 0 else None
    return max_levels, max_nodes


def _collect_value_component_nodes(
    model: BuildResult,
    root: ModelNode,
    max_depth: int,
    max_levels: int | None = None,
    max_nodes: int | None = None,
) -> tuple[list[ModelNode], bool]:
    """Collect composition property descendants and report whether server limits truncated them.

    ``maxDepth`` counts composition levels (spec IMPLEMENTATION_GUIDE.md:1105-1109): ``1`` = no
    recursion, ``N > 1`` = up to N levels, ``0`` = unlimited. Unlimited requests are still bound by
    the server's own limits (``max_levels`` / ``max_nodes``, ``None`` = unbounded), and the second
    return value says that the recursion stopped short of what the client asked for - the routes
    surface that as HTTP 206 rather than an incomplete-but-200 result (spec :1113-1119).
    """
    if max_depth == 1:
        return [], False

    requested_levels = max_depth if max_depth > 0 else None
    level_limit = requested_levels
    if max_levels is not None:
        level_limit = min(requested_levels, max_levels) if requested_levels is not None else max_levels
    limited_by_server = level_limit is not None and (requested_levels is None or requested_levels > level_limit)

    components: list[ModelNode] = []
    queue: deque[tuple[str, int]] = deque([(root.id, 0)])
    visited: set[str] = set()
    truncated = False

    while queue:
        node_id, depth = queue.popleft()
        if node_id in visited:
            continue
        visited.add(node_id)

        current_node = model.nodes_by_id.get(node_id)
        if current_node is None:
            continue

        children = _composition_children_for_node(model, current_node)
        if not children:
            continue

        if level_limit is not None and depth >= level_limit:
            # Composition children exist below this level and were not fetched.
            truncated = truncated or limited_by_server
            continue

        for child_id in children:
            child = model.nodes_by_id.get(child_id)
            if child is None:
                continue
            if child.kind == "property":
                components.append(child)
            else:
                queue.append((child.id, depth + 1))

        if max_nodes is not None and len(components) >= max_nodes and queue:
            truncated = True
            queue.clear()
            break

    return components, truncated


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


def _to_vqt_from_history_value(data_value: Any) -> VQT:
    variant = getattr(data_value, "Value", None)
    value = getattr(variant, "Value", variant)
    timestamp = (
        getattr(data_value, "SourceTimestamp", None)
        or getattr(data_value, "ServerTimestamp", None)
        or getattr(data_value, "timestamp", None)
    )
    quality = _normalize_quality(getattr(data_value, "StatusCode", None) or getattr(data_value, "status", None))
    safe_value = _to_json_safe_value(value)
    if safe_value is None and quality not in ("Bad", "GoodNoData"):
        quality = "GoodNoData"
    return VQT(value=safe_value, quality=quality, timestamp=_normalize_timestamp(timestamp))


def _increment_counter(counter: dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def _build_model_metrics(model: BuildResult) -> ModelMetricsResponse:
    cached_metrics = model._model_metrics_cache
    if cached_metrics is not None:
        return cast(ModelMetricsResponse, cached_metrics)

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

    metrics = ModelMetricsResponse(
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
    model._model_metrics_cache = metrics
    return metrics
