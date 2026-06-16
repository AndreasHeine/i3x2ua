"""Shared mapping helpers for subscription DTOs."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TypedDict

from i3x_server.application.ports.subscription import (
    SubscriptionDeleteResultPort,
    SubscriptionDetailPort,
    SubscriptionSyncResultPort,
    SubscriptionUpdatePort,
)


class CreateSubscriptionDto(TypedDict):
    subscriptionId: str
    clientId: str
    displayName: str | None


class RegisterMonitoredItemsDto(TypedDict):
    subscriptionId: str
    monitoredItems: list[str]
    registered: int


class SubscriptionUpdateDto(TypedDict):
    sequenceNumber: int
    elementId: str
    nodeId: str
    value: object
    quality: str
    timestamp: str


class PublicSubscriptionUpdateDto(TypedDict):
    sequenceNumber: int
    elementId: str
    value: object
    quality: str
    timestamp: str


class PendingUpdatesDto(TypedDict):
    updates: list[SubscriptionUpdateDto]
    queueOverflow: bool
    droppedFromSequence: int | None
    droppedToSequence: int | None


class DeleteSubscriptionsDto(TypedDict):
    deleted: int
    requested: int


class SubscriptionBulkItemDto(TypedDict):
    success: bool
    elementId: str
    subscriptionId: str
    result: object | None
    error: SubscriptionDeleteResultPort.ErrorPayload | None


class SubscriptionDetailDto(TypedDict):
    subscriptionId: str
    clientId: str | None
    displayName: str | None
    monitoredObjects: list[dict[str, object]]
    mode: str


class PublicSubscriptionBatchDto(TypedDict):
    sequenceNumber: int
    updates: list[PublicSubscriptionUpdateDto]


class ListSubscriptionsDto(TypedDict):
    subscriptions: list[SubscriptionDetailDto]


def map_create_subscription(
    created: SubscriptionDetailPort,
    client_id: str,
    display_name: str | None,
) -> CreateSubscriptionDto:
    return {
        "subscriptionId": created.subscription_id,
        "clientId": client_id,
        "displayName": display_name,
    }


def map_register_monitored_items(
    subscription_id: str,
    element_ids: list[str],
) -> RegisterMonitoredItemsDto:
    return {
        "subscriptionId": subscription_id,
        "monitoredItems": element_ids,
        "registered": len(element_ids),
    }


def map_subscription_update(item: SubscriptionUpdatePort) -> SubscriptionUpdateDto:
    return {
        "sequenceNumber": item.sequence_number,
        "elementId": item.element_id,
        "nodeId": item.node_id,
        "value": item.value,
        "quality": item.quality,
        "timestamp": item.timestamp,
    }


def map_pending_updates(sync_result: SubscriptionSyncResultPort) -> PendingUpdatesDto:
    return {
        "updates": [map_subscription_update(item) for item in sync_result.updates],
        "queueOverflow": sync_result.queue_overflow,
        "droppedFromSequence": sync_result.dropped_from_sequence,
        "droppedToSequence": sync_result.dropped_to_sequence,
    }


def map_public_subscription_update(
    item: SubscriptionUpdatePort,
    element_id_mapper: Callable[[str], str] | None = None,
    value_mapper: Callable[[object], object] | None = None,
) -> PublicSubscriptionUpdateDto:
    map_element_id = element_id_mapper or (lambda element_id: element_id)
    map_value = value_mapper or (lambda value: value)
    return {
        "sequenceNumber": item.sequence_number,
        "elementId": map_element_id(item.element_id),
        "value": map_value(item.value),
        "quality": item.quality,
        "timestamp": item.timestamp,
    }


def map_public_subscription_updates(
    updates: Iterable[SubscriptionUpdatePort],
    element_id_mapper: Callable[[str], str] | None = None,
    value_mapper: Callable[[object], object] | None = None,
) -> list[PublicSubscriptionUpdateDto]:
    return [
        map_public_subscription_update(item, element_id_mapper=element_id_mapper, value_mapper=value_mapper)
        for item in updates
    ]


def map_public_sync_batches(
    sync_result: SubscriptionSyncResultPort,
    element_id_mapper: Callable[[str], str] | None = None,
    value_mapper: Callable[[object], object] | None = None,
) -> list[PublicSubscriptionBatchDto]:
    updates = map_public_subscription_updates(
        sync_result.updates,
        element_id_mapper=element_id_mapper,
        value_mapper=value_mapper,
    )
    if not updates:
        return []
    return [
        {
            "sequenceNumber": sync_result.updates[-1].sequence_number,
            "updates": updates,
        }
    ]


def map_delete_subscriptions(
    delete_results: list[SubscriptionDeleteResultPort],
    requested: int,
) -> DeleteSubscriptionsDto:
    deleted_count = sum(1 for item in delete_results if item.success)
    return {"deleted": deleted_count, "requested": requested}


def _subscription_bulk_success(
    element_id: str,
    subscription_id: str,
    result: object | None = None,
) -> SubscriptionBulkItemDto:
    """Create a successful subscription bulk response item."""
    return {
        "success": True,
        "elementId": element_id,
        "subscriptionId": subscription_id,
        "result": result,
        "error": None,
    }


def _subscription_bulk_error(
    element_id: str,
    subscription_id: str,
    error: SubscriptionDeleteResultPort.ErrorPayload | None = None,
) -> SubscriptionBulkItemDto:
    """Create an error subscription bulk response item."""
    return {
        "success": False,
        "elementId": element_id,
        "subscriptionId": subscription_id,
        "result": None,
        "error": error,
    }


def map_delete_subscription_items(
    delete_results: Iterable[SubscriptionDeleteResultPort],
) -> list[SubscriptionBulkItemDto]:
    return [
        _subscription_bulk_success(item.subscription_id, item.subscription_id)
        if item.success
        else _subscription_bulk_error(item.subscription_id, item.subscription_id, item.error)
        for item in delete_results
    ]


def map_subscription_detail(
    item: SubscriptionDetailPort,
    element_id_mapper: Callable[[str], str] | None = None,
) -> SubscriptionDetailDto:
    map_element_id = element_id_mapper or (lambda element_id: element_id)
    monitored_objects: list[dict[str, object]] = []
    for monitored in item.monitored_objects:
        mapped = {**monitored}
        raw_element_id = str(monitored.get("elementId", ""))
        mapped["elementId"] = map_element_id(raw_element_id)
        monitored_objects.append(mapped)

    return {
        "subscriptionId": item.subscription_id,
        "clientId": item.client_id,
        "displayName": item.display_name,
        "monitoredObjects": monitored_objects,
        "mode": item.mode,
    }


def map_list_subscriptions(
    subscriptions: Iterable[SubscriptionDetailPort],
    element_id_mapper: Callable[[str], str] | None = None,
) -> ListSubscriptionsDto:
    return {
        "subscriptions": [map_subscription_detail(item, element_id_mapper=element_id_mapper) for item in subscriptions]
    }


def map_subscription_detail_bulk_items(
    subscriptions: Iterable[SubscriptionDetailPort],
    requested_ids: list[str] | None = None,
    element_id_mapper: Callable[[str], str] | None = None,
) -> list[SubscriptionBulkItemDto]:
    details: list[SubscriptionDetailDto] = [
        map_subscription_detail(item, element_id_mapper=element_id_mapper) for item in subscriptions
    ]
    detail_by_id = {item["subscriptionId"]: item for item in details}

    if requested_ids is None:
        return [_subscription_bulk_success(item["subscriptionId"], item["subscriptionId"], item) for item in details]

    results: list[SubscriptionBulkItemDto] = []
    for subscription_id in requested_ids:
        detail = detail_by_id.get(subscription_id)
        if detail is None:
            results.append(
                _subscription_bulk_error(
                    subscription_id,
                    subscription_id,
                    {"code": 404, "message": f"Subscription not found: {subscription_id}"},
                )
            )
            continue
        results.append(_subscription_bulk_success(subscription_id, subscription_id, detail))

    return results
