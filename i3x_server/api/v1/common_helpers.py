"""Cross-feature v1 helpers: bulk envelopes, node/namespace resolution, error mapping.

These are presentation-local helpers shared by the ``/v1`` route modules. Anything that is a
domain rule (node id and namespace arithmetic) comes from ``domain.utils``; anything that is a
wire format lives in ``contracts``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, cast

from fastapi import Request
from typing_extensions import Never

from i3x_server.api.v1.contracts import VQT, BulkResultItem, ErrorDetail, SubscriptionDetail, T
from i3x_server.api.v1.object_helpers import _find_model_node
from i3x_server.application.ports.opcua import OpcUaClientProtocol, OpcUaNamespaceInfo
from i3x_server.application.services.subscription_mapper import SubscriptionBulkItemDto
from i3x_server.config.settings import trust_principal_header
from i3x_server.domain.ports.opcua import Quality
from i3x_server.domain.utils import expanded_node_id
from i3x_server.errors import i3x_http_error
from i3x_server.schemas.opcua_value import now_iso, to_json_safe_value
from i3x_server.schemas.state import BuildResult


def _resolve_model_nodes(model: BuildResult, element_ids: list[str]) -> list[tuple[str, Any | None]]:
    """Pair each requested element id with its model node (``None`` when it is unknown)."""
    return [(element_id, _find_model_node(model, element_id)) for element_id in element_ids]


def _audit_principal(request: Request) -> str:
    """Audit principal for a request, gated by ``I3X_TRUST_PRINCIPAL_HEADER``.

    The header is attacker-controlled, so it is only trusted when the deployment opts in;
    otherwise every request audits as ``anonymous``.
    """
    if not trust_principal_header():
        return "anonymous"
    return request.headers.get("x-principal") or "anonymous"


def _namespace_index_from_node_id(node_id: str) -> int | None:
    """Namespace index carried by a node id, or ``None`` when the id names no namespace.

    An id without an ``ns=`` prefix lives in namespace 0 (OPC UA Part 6, 5.3.1), so ids that
    start with an identifier type are reported as index 0 rather than as unresolvable.
    """
    match = re.match(r"^ns=(\d+);", node_id)
    if match is None:
        if re.match(r"^[isgb]=", node_id):
            return 0
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _raise_invalid_argument(
    field_name: str,
    value: object | None = None,
    message: str | None = None,
) -> Never:
    """Fail the request with a 400 ``InvalidArgument`` naming the offending field."""
    if message is None:
        message = f"Invalid value for '{field_name}'"
    raise i3x_http_error(
        400,
        "InvalidArgument",
        message,
        {"field": field_name, "value": value},
    )


def _raise_not_found(entity_type: str, entity_id: str) -> Never:
    """Fail the request with a 404 ``NotFound`` naming the entity that is missing."""
    raise i3x_http_error(
        404,
        "NotFound",
        f"{entity_type} '{entity_id}' not found",
    )


def _raise_opcua_error(operation: str, cause: Exception | str) -> Never:
    """Fail the request with a 502 ``OpcUaError``; the upstream failure stays the cause."""
    cause_str = str(cause) if isinstance(cause, Exception) else cause
    raise i3x_http_error(
        502,
        "OpcUaError",
        f"Failed to {operation}",
        {"cause": cause_str},
    ) from (cause if isinstance(cause, Exception) else None)


def _not_implemented(feature: str) -> Never:
    """Reject a feature this gateway does not implement (501, naming the feature)."""
    raise i3x_http_error(
        501,
        "NotImplemented",
        f"{feature} is not implemented in this server",
        {"feature": feature},
    )


async def _fetch_namespace_infos(opcua_client: OpcUaClientProtocol) -> list[OpcUaNamespaceInfo]:
    """Namespace infos for a response, degrading to empty when the server cannot answer.

    Namespace lookups only refine node ids in responses; a failing lookup must not turn a
    readable endpoint into a 502, so the failure is swallowed here.
    """
    try:
        return await opcua_client.get_namespace_infos()
    except Exception:
        return []


def _validate_subscription_element_ids(
    model: BuildResult,
    element_ids: list[str],
) -> tuple[list[str], list[BulkResultItem[None]]]:
    """Split requested element ids into the known ones and per-item bulk failures.

    The bulk envelope reports every id, so the caller gets both the ids to act on and the
    items to return for the ids that did not resolve.
    """
    from i3x_server.api.v1.contracts import _bulk_result_error, _bulk_result_success

    known_ids: list[str] = []
    results: list[BulkResultItem[None]] = []
    for element_id, node in _resolve_model_nodes(model, element_ids):
        if node is None:
            results.append(_bulk_result_error(element_id, f"Element not found: {element_id}"))
            continue
        known_ids.append(element_id)
        results.append(_bulk_result_success(element_id, None))
    return known_ids, results


def _map_subscription_result(
    result: object | None,
    result_mapper: Callable[[object], T] | None = None,
) -> T | None:
    """Map a port result into the response type, passing it through when there is no mapper."""
    if result is None:
        return None
    if result_mapper is None:
        # ``result`` arrives as ``object`` from the port; the mapper-less path hands it through
        # unchanged, so the caller's ``T`` binding is the only available type evidence.
        return cast(T, result)
    return result_mapper(result)


def _error_detail_from_payload(payload: Mapping[str, object] | None) -> ErrorDetail | None:
    """Coerce a port error payload (`Mapping[str, object]`) into the response model."""
    if payload is None:
        return None
    raw_code: object = payload["code"]
    code = raw_code if isinstance(raw_code, int) else int(str(raw_code))
    return ErrorDetail(code=code, message=str(payload["message"]))


def _map_subscription_bulk_items_to_result_items(
    items: list[SubscriptionBulkItemDto],
    result_mapper: Callable[[object], T] | None = None,
) -> list[BulkResultItem[T]]:
    """Turn port bulk items into response items, applying ``result_mapper`` where given."""
    return [
        BulkResultItem[T](
            success=item["success"],
            elementId=item["elementId"],
            subscriptionId=item["subscriptionId"],
            result=_map_subscription_result(item["result"], result_mapper),
            error=_error_detail_from_payload(item["error"]),
        )
        for item in items
    ]


def _map_delete_subscription_bulk_result_items(items: list[SubscriptionBulkItemDto]) -> list[BulkResultItem[None]]:
    """Delete results: nothing to report per item beyond success/error."""
    return _map_subscription_bulk_items_to_result_items(items)


def _map_subscription_detail_bulk_result_items(
    items: list[SubscriptionBulkItemDto],
) -> list[BulkResultItem[SubscriptionDetail]]:
    """Read results whose payload is the subscription detail the port returned."""
    return _map_subscription_bulk_items_to_result_items(
        items,
        result_mapper=SubscriptionDetail.model_validate,
    )


def _expand_subscription_bulk_item_element_ids(
    items: list[SubscriptionBulkItemDto],
    namespace_infos: list[OpcUaNamespaceInfo],
) -> list[SubscriptionBulkItemDto]:
    """Expand the element ids inside monitored objects to their ``nsu=`` form.

    Monitored items are reported with the upstream node id; the response contract publishes
    expanded ids, so the mapping happens on the copy rather than the port's payload.
    """
    expanded: list[SubscriptionBulkItemDto] = []
    for item in items:
        mapped = dict(item)
        result = item.get("result")
        if isinstance(result, dict):
            result_copy = dict(result)
            monitored_objects = result_copy.get("monitoredObjects")
            if isinstance(monitored_objects, list):
                mapped_monitored_objects: list[dict[str, object]] = []
                for monitored in monitored_objects:
                    if not isinstance(monitored, dict):
                        continue
                    monitored_copy = dict(monitored)
                    element_id = monitored_copy.get("elementId")
                    if isinstance(element_id, str):
                        monitored_copy["elementId"] = expanded_node_id(element_id, namespace_infos)
                    mapped_monitored_objects.append(monitored_copy)
                result_copy["monitoredObjects"] = mapped_monitored_objects
            mapped["result"] = result_copy
        expanded.append(cast(SubscriptionBulkItemDto, mapped))
    return expanded


def _to_json_safe_value(value: Any) -> Any:
    """JSON-safe payload for presentation responses.

    The presentation layer drops ``encoding`` fields: publishing the OPC UA encoding type
    next to the value is noise the i3X contract does not define.
    """
    return to_json_safe_value(value, exclude_encoding_fields=True)


def _good_no_data_vqt() -> VQT:
    """VQT for an element that has no readable value: Good severity, no payload."""
    return VQT(value=None, quality=Quality.GOOD_NO_DATA, timestamp=now_iso())
