from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from typing import Any, cast

from pydantic import BaseModel
from typing_extensions import Never

from i3x_server.api.v1.contracts import VQT, BulkResultItem, ErrorDetail, SubscriptionDetail, T
from i3x_server.api.v1.object_helpers import _expanded_node_id, _find_model_node
from i3x_server.application.ports.opcua import OpcUaClientProtocol, OpcUaNamespaceInfo
from i3x_server.application.services.subscription_mapper import SubscriptionBulkItemDto
from i3x_server.errors import i3x_http_error
from i3x_server.schemas.state import BuildResult


def _resolve_model_nodes(model: BuildResult, element_ids: list[str]) -> list[tuple[str, Any | None]]:
    return [(element_id, _find_model_node(model, element_id)) for element_id in element_ids]


def _raise_invalid_argument(
    field_name: str,
    value: object | None = None,
    message: str | None = None,
) -> Never:
    if message is None:
        message = f"Invalid value for '{field_name}'"
    raise i3x_http_error(
        400,
        "InvalidArgument",
        message,
        {"field": field_name, "value": value},
    )


def _raise_not_found(entity_type: str, entity_id: str) -> Never:
    raise i3x_http_error(
        404,
        "NotFound",
        f"{entity_type} '{entity_id}' not found",
    )


def _raise_opcua_error(operation: str, cause: Exception | str) -> Never:
    cause_str = str(cause) if isinstance(cause, Exception) else cause
    raise i3x_http_error(
        502,
        "OpcUaError",
        f"Failed to {operation}",
        {"cause": cause_str},
    ) from (cause if isinstance(cause, Exception) else None)


async def _fetch_namespace_infos(opcua_client: OpcUaClientProtocol) -> list[OpcUaNamespaceInfo]:
    try:
        return await opcua_client.get_namespace_infos()
    except Exception:
        return []


def _validate_subscription_element_ids(
    model: BuildResult,
    element_ids: list[str],
) -> tuple[list[str], list[BulkResultItem[None]]]:
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
    if result is None:
        return None
    if result_mapper is None:
        return result  # type: ignore
    return result_mapper(result)


def _map_subscription_bulk_items_to_result_items(
    items: list[SubscriptionBulkItemDto],
    result_mapper: Callable[[object], T] | None = None,
) -> list[BulkResultItem[T]]:
    return [
        BulkResultItem[T](
            success=item["success"],
            elementId=item["elementId"],
            subscriptionId=item["subscriptionId"],
            result=_map_subscription_result(item["result"], result_mapper),
            error=None if item["error"] is None else ErrorDetail(**item["error"]),
        )
        for item in items
    ]


def _map_delete_subscription_bulk_result_items(items: list[SubscriptionBulkItemDto]) -> list[BulkResultItem[None]]:
    return _map_subscription_bulk_items_to_result_items(items)


def _map_subscription_detail_bulk_result_items(
    items: list[SubscriptionBulkItemDto],
) -> list[BulkResultItem[SubscriptionDetail]]:
    return _map_subscription_bulk_items_to_result_items(
        items,
        result_mapper=SubscriptionDetail.model_validate,
    )


def _expand_subscription_bulk_item_element_ids(
    items: list[SubscriptionBulkItemDto],
    namespace_infos: list[OpcUaNamespaceInfo],
) -> list[SubscriptionBulkItemDto]:
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
                        monitored_copy["elementId"] = _expanded_node_id(element_id, namespace_infos)
                    mapped_monitored_objects.append(monitored_copy)
                result_copy["monitoredObjects"] = mapped_monitored_objects
            mapped["result"] = result_copy
        expanded.append(cast(SubscriptionBulkItemDto, mapped))
    return expanded


def _require_client_id(client_id: str | None, endpoint: str) -> str:
    normalized = (client_id or "").strip()
    if normalized:
        return normalized
    _raise_invalid_argument(
        "clientId",
        None,
        f"'{endpoint}' requires a non-empty clientId",
    )


def _format_utc_timestamp(value: datetime) -> str:
    normalized = value
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now_iso() -> str:
    return _format_utc_timestamp(datetime.now(timezone.utc))


def _to_json_safe_value(value: Any) -> Any:
    def _is_filtered_structure_field(name: str) -> bool:
        return name.strip().lower() == "encoding"

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return _format_utc_timestamp(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        encoded = base64.b64encode(bytes(value)).decode("ascii")
        return {"encoding": "base64", "data": encoded}
    if hasattr(value, "TypeId") and hasattr(value, "Body"):
        body = value.Body
        if body is None:
            return None
        return {
            "TypeId": _to_json_safe_value(value.TypeId),
            "Body": _to_json_safe_value(body),
        }
    if isinstance(value, BaseModel):
        return _to_json_safe_value(value.model_dump(mode="json", by_alias=True))
    if is_dataclass(value):
        return {
            item.name: _to_json_safe_value(getattr(value, item.name))
            for item in fields(value)
            if not _is_filtered_structure_field(item.name)
        }
    if isinstance(value, (list, tuple, set)):
        return [_to_json_safe_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_json_safe_value(item) for key, item in value.items()}
    if hasattr(value, "__dict__") and type(value).__module__ != "builtins":
        return {
            str(key): _to_json_safe_value(item)
            for key, item in vars(value).items()
            if not key.startswith("_") and not callable(item) and not _is_filtered_structure_field(str(key))
        }
    return str(value)


def _good_no_data_vqt() -> VQT:
    return VQT(value=None, quality="GoodNoData", timestamp=_now_iso())
