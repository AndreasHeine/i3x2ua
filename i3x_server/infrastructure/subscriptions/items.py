"""Monitored-item registration, unregistration, and sync for one subscription.

Owns the mapping plumbing (model revision -> source index -> node ids/element ids) and
the reconciliation of the shared node registry against a subscription's desired set.
Network work stays outside the registry lock; helpers are reached through the service so
patching a service method keeps affecting every path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from i3x_server.domain.errors import AdmissionLimitError
from i3x_server.infrastructure.subscriptions import mapping, state
from i3x_server.infrastructure.subscriptions.state import SubscriptionSyncResult
from i3x_server.model.revision import model_revision
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult

if TYPE_CHECKING:
    from i3x_server.infrastructure.subscriptions.service import SubscriptionService

logger = logging.getLogger(__name__)


async def _register_items(
    service: SubscriptionService,
    client_id: str | None,
    subscription_id: str,
    element_ids: list[str],
    max_depth: int,
    model: BuildResult,
) -> bool:
    service._ensure_cleanup_task()
    requested_count = len(element_ids)
    previous_monitored_nodes = 0
    monitored_nodes_count = 0
    monitored_objects_count = 0
    async with service._lock:
        subscription_state = service._subscriptions.get(subscription_id)
        if subscription_state is None or (client_id is not None and subscription_state.client_id != client_id):
            return False
        previous_node_ids = set(subscription_state.monitored_node_ids)
        previous_monitored_nodes = len(previous_node_ids)
        token = model_revision(model)
        source_index = service._source_index(token, model)
        budget = service._subtree_max_nodes
        # Mappings are built into prospective containers and committed only after the budget
        # check. A rejected registration must leave the previous monitored set intact -
        # truncating silently would report monitoring that never happens upstream.
        depths = dict(subscription_state.monitored_objects)
        if subscription_state.mapping_model_token != token:
            # A refreshed model invalidates every mapping built against the previous revision.
            depths.update({element_id: max_depth for element_id in element_ids})
            object_mappings = {
                element_id: mapping._resolve_object_mappings(element_id, depth, model, source_index, max_nodes=budget)
                for element_id, depth in depths.items()
            }
        else:
            object_mappings = dict(subscription_state.object_mappings)
            for element_id in element_ids:
                known_depth = depths.get(element_id)
                if known_depth == max_depth and element_id in subscription_state.object_mappings:
                    continue
                depths[element_id] = max_depth
                object_mappings[element_id] = mapping._resolve_object_mappings(
                    element_id, max_depth, model, source_index, max_nodes=budget
                )
        monitored_node_ids, node_to_element_id = service._merge_object_mappings(object_mappings)
        if len(monitored_node_ids) > budget:
            raise AdmissionLimitError(
                400,
                "MonitoredItemBudget",
                "Monitored item budget exceeded",
                {
                    "subscriptionId": subscription_id,
                    "monitoredNodes": len(monitored_node_ids),
                    "maxNodes": budget,
                },
            )
        subscription_state.monitored_objects = depths
        subscription_state.mapping_model_token = token
        subscription_state.object_mappings = object_mappings
        subscription_state.monitored_node_ids = monitored_node_ids
        subscription_state.node_to_element_id = node_to_element_id
        monitored_nodes_count = len(monitored_node_ids)
        monitored_objects_count = len(subscription_state.monitored_objects)
        # Pruning the last-value maps must not race with concurrent appends, which mutate
        # those maps under the subscription's lock (registry -> state acquisition order).
        async with subscription_state.lock:
            subscription_state.last_values_by_node_id = {
                node_id: value
                for node_id, value in subscription_state.last_values_by_node_id.items()
                if node_id in monitored_node_ids
            }
            subscription_state.last_quality_by_node_id = {
                node_id: quality
                for node_id, quality in subscription_state.last_quality_by_node_id.items()
                if node_id in subscription_state.last_values_by_node_id
            }
        service._touch(subscription_state)

    await service._apply_monitored_node_diff(subscription_id)
    logger.info(
        "Subscription monitored items updated subscription_id=%s requested=%d total_objects=%d "
        "monitored_nodes=%d previous_monitored_nodes=%d max_depth=%d",
        subscription_id,
        requested_count,
        monitored_objects_count,
        monitored_nodes_count,
        previous_monitored_nodes,
        max_depth,
    )
    return True


async def _unregister_items(
    service: SubscriptionService,
    client_id: str | None,
    subscription_id: str,
    element_ids: list[str],
    model: BuildResult,
) -> bool:
    service._ensure_cleanup_task()
    requested_count = len(element_ids)
    previous_monitored_nodes = 0
    monitored_nodes_count = 0
    monitored_objects_count = 0
    async with service._lock:
        subscription_state = service._subscriptions.get(subscription_id)
        if subscription_state is None or (client_id is not None and subscription_state.client_id != client_id):
            return False
        previous_node_ids = set(subscription_state.monitored_node_ids)
        previous_monitored_nodes = len(previous_node_ids)
        for element_id in element_ids:
            subscription_state.monitored_objects.pop(element_id, None)
        token = model_revision(model)
        if subscription_state.mapping_model_token != token:
            # A refreshed model invalidates every mapping built against the previous revision.
            subscription_state.mapping_model_token = token
            source_index = service._source_index(token, model)
            subscription_state.object_mappings = {
                element_id: mapping._resolve_object_mappings(element_id, depth, model, source_index)
                for element_id, depth in subscription_state.monitored_objects.items()
            }
        else:
            for element_id in element_ids:
                subscription_state.object_mappings.pop(element_id, None)
        monitored_node_ids, node_to_element_id = service._merge_object_mappings(subscription_state.object_mappings)
        subscription_state.monitored_node_ids = monitored_node_ids
        subscription_state.node_to_element_id = node_to_element_id
        monitored_nodes_count = len(monitored_node_ids)
        monitored_objects_count = len(subscription_state.monitored_objects)
        # Pruning the last-value maps must not race with concurrent appends, which mutate
        # those maps under the subscription's lock (registry -> state acquisition order).
        async with subscription_state.lock:
            subscription_state.last_values_by_node_id = {
                node_id: value
                for node_id, value in subscription_state.last_values_by_node_id.items()
                if node_id in monitored_node_ids
            }
            subscription_state.last_quality_by_node_id = {
                node_id: quality
                for node_id, quality in subscription_state.last_quality_by_node_id.items()
                if node_id in subscription_state.last_values_by_node_id
            }
        service._touch(subscription_state)

    await service._apply_monitored_node_diff(subscription_id)
    logger.info(
        "Subscription monitored items removed subscription_id=%s requested=%d total_objects=%d "
        "monitored_nodes=%d previous_monitored_nodes=%d",
        subscription_id,
        requested_count,
        monitored_objects_count,
        monitored_nodes_count,
        previous_monitored_nodes,
    )
    return True


async def _sync(
    service: SubscriptionService,
    client_id: str | None,
    subscription_id: str,
    acknowledge_sequence: int | None = None,
    allow_when_stream_active: bool = False,
) -> SubscriptionSyncResult | None:
    service._ensure_cleanup_task()
    should_refresh = False
    async with service._lock:
        subscription_state = service._subscriptions.get(subscription_id)
        if subscription_state is None or (client_id is not None and subscription_state.client_id != client_id):
            return None

        if subscription_state.stream_connected and not allow_when_stream_active:
            # Keep the TTL refreshed: a client polling while a stream is open is still active,
            # otherwise the subscription expires mid-stream (400 "stream is active" until TTL).
            # Report the overflow markers without consuming them: polling can see dropped
            # updates while the stream delivers a silent sequence jump.
            service._touch(subscription_state)
            async with subscription_state.lock:
                return SubscriptionSyncResult(
                    updates=[],
                    stream_active=True,
                    queue_overflow=subscription_state.dropped_from_sequence is not None,
                    dropped_from_sequence=subscription_state.dropped_from_sequence,
                    dropped_to_sequence=subscription_state.dropped_to_sequence,
                )

        # Buffer, sequence counters, and drop markers change under the subscription's own
        # lock (nesting registry -> state, the fixed acquisition order).
        async with subscription_state.lock:
            if acknowledge_sequence is not None and acknowledge_sequence < 0:
                # Any negative ack is a "drop everything" sentinel; `-1` is documented, but
                # other negatives used to fall through and discard nothing.
                subscription_state.updates.clear()
            elif isinstance(acknowledge_sequence, int):
                if 0 <= acknowledge_sequence <= subscription_state.sequence_number:
                    # Sequences strictly increase along the buffer, so trimming the head is
                    # equivalent to filtering - and costs O(acked) popleft instead of
                    # copying the whole buffer.
                    updates = subscription_state.updates
                    while updates and updates[0].sequence_number <= acknowledge_sequence:
                        updates.popleft()

            should_refresh = (
                acknowledge_sequence is None
                and not subscription_state.updates
                and bool(subscription_state.monitored_node_ids)
            )

        service._touch(subscription_state)

    if should_refresh:
        await service._refresh_changed_values(subscription_id)

    async with service._lock:
        subscription_state = service._subscriptions.get(subscription_id)
        if subscription_state is None or (client_id is not None and subscription_state.client_id != client_id):
            return None
        service._touch(subscription_state)
        async with subscription_state.lock:
            result = SubscriptionSyncResult(
                updates=list(subscription_state.updates),
                queue_overflow=subscription_state.dropped_from_sequence is not None,
                dropped_from_sequence=subscription_state.dropped_from_sequence,
                dropped_to_sequence=subscription_state.dropped_to_sequence,
            )
            subscription_state.dropped_from_sequence = None
            subscription_state.dropped_to_sequence = None
        return result


async def _apply_monitored_node_diff(service: SubscriptionService, subscription_id: str) -> None:
    """Reconcile the shared node registry against the subscription's *current* desired set.

    Recomputed under the lock from ``applied_node_ids`` (what is actually monitored), so a
    concurrent register/unregister cannot leave an acquired node that the final desired set
    no longer contains. Network calls happen outside the lock.
    """
    async with service._lock:
        subscription_state = service._subscriptions.get(subscription_id)
        if subscription_state is None:
            return
        desired = set(subscription_state.monitored_node_ids)
        removed = subscription_state.applied_node_ids - desired
        added = desired - subscription_state.applied_node_ids
        subscription_state.applied_node_ids = desired

    for node_id in removed:
        await service._release_node_monitor(node_id, subscription_id)
    if added:
        # Concurrent admissions: bin selection stays serialized (packing), the OPC UA
        # round-trips run under the native admission semaphore, same as cross-subscription
        # registrations already do.
        await asyncio.gather(*(service._acquire_node_monitor(node_id, subscription_id) for node_id in sorted(added)))

    async with service._lock:
        subscription_state = service._subscriptions.get(subscription_id)
        if subscription_state is None:
            vanished = True
            orphaned: set[str] = set()
        else:
            vanished = False
            # A concurrent diff may have removed nodes while these acquisitions were in
            # flight and released nothing (monitors did not exist yet). Reconcile now so
            # the applied-vs-desired invariant holds regardless of scheduling order.
            orphaned = added - subscription_state.applied_node_ids
    if vanished:
        # Subscription was deleted while the diff was in flight: undo the acquisitions.
        for node_id in added:
            await service._release_node_monitor(node_id, subscription_id)
        return

    if orphaned:
        await asyncio.gather(*(service._release_node_monitor(node_id, subscription_id) for node_id in sorted(orphaned)))

    await service._update_subscription_mode(subscription_id)
    await service._seed_initial_updates(subscription_id)


async def _release_all_nodes(service: SubscriptionService, subscription_state: state._SubscriptionState) -> None:
    # Release everything that was ever acquired, not just the final desired set.
    for node_id in list(subscription_state.applied_node_ids | subscription_state.monitored_node_ids):
        await service._release_node_monitor(node_id, subscription_state.subscription_id)
    async with service._lock:
        subscription_state.mode = "idle"
        subscription_state.native_refresh_interval_seconds = None


def _resolve_target_node(subscription_state: state._SubscriptionState, node_id: str) -> str | None:
    """Resolve the monitored node an incoming notification refers to.

    Must be called under the service lock: ``monitored_node_ids`` and ``node_to_element_id``
    are registry-guarded, so the lookup cannot happen while the subscription lock is held.
    """
    if node_id in subscription_state.monitored_node_ids:
        return node_id
    mapped_element = subscription_state.node_to_element_id.get(node_id)
    if mapped_element is None:
        mapped_element = subscription_state.node_to_element_id.get(node_id.lower())
    if mapped_element is None and not subscription_state.monitored_node_ids:
        return None
    # A single monitored node accepts any notification the server reports for
    # it (node-id formats can differ); with several nodes an unmapped id is dropped.
    if len(subscription_state.monitored_node_ids) == 1:
        return next(iter(subscription_state.monitored_node_ids))
    if mapped_element is None:
        return None
    return node_id


def _resolve_element_id(subscription_state: state._SubscriptionState, node_id: str) -> str:
    """Map a node id to the element id reported in updates. Registry lock."""
    element_id = subscription_state.node_to_element_id.get(node_id)
    if element_id is None:
        element_id = subscription_state.node_to_element_id.get(node_id.lower(), node_id)
    if element_id == node_id and len(subscription_state.monitored_objects) == 1:
        # Compatibility fallback: if native callback mapping misses for a
        # single registered object, emit that object id so consumers can
        # still match updates to the active subscription row.
        element_id = next(iter(subscription_state.monitored_objects.keys()))
    return element_id


def _source_index(service: SubscriptionService, token: str, model: BuildResult) -> dict[str, ModelNode]:
    """Model source-node lookup cached per model revision token."""
    cached = service._source_index_cache
    if cached is None or cached[0] != token:
        cached = (token, mapping._build_source_index(model))
        service._source_index_cache = cached
    return cached[1]


def _merge_object_mappings(object_mappings: dict[str, dict[str, str]]) -> tuple[set[str], dict[str, str]]:
    """Fold per-object mappings into the monitored node set and the case-folded lookup."""
    node_ids: set[str] = set()
    node_to_element_id: dict[str, str] = {}
    for mappings in object_mappings.values():
        node_ids.update(mappings)
        node_to_element_id.update(mappings)
        node_to_element_id.update({key.lower(): value for key, value in mappings.items()})
    return node_ids, node_to_element_id


def _resolve_monitored_node_ids(
    service: SubscriptionService,
    monitored_objects: dict[str, int],
    model: BuildResult,
    max_nodes: int | None = None,
) -> tuple[set[str], dict[str, str]]:
    # ``max_nodes`` defaults to the service's subtree budget.
    return mapping._resolve_monitored_node_ids(
        monitored_objects, model, max_nodes=service._subtree_max_nodes if max_nodes is None else max_nodes
    )


def _collect_property_source_mappings(
    service: SubscriptionService,
    model: BuildResult,
    root: ModelNode,
    max_depth: int,
    max_nodes: int | None = None,
) -> dict[str, str]:
    # ``max_nodes`` defaults to the service's subtree budget.
    return mapping._collect_property_source_mappings(
        model, root, max_depth, max_nodes=service._subtree_max_nodes if max_nodes is None else max_nodes
    )
