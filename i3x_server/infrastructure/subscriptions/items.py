"""Monitored-item registration, unregistration, and sync for one subscription.

Owns the mapping plumbing (model revision -> source index -> node ids/element ids) and
the reconciliation of the shared node registry against a subscription's desired set.
Mapping resolution and network work stay outside the registry lock - the locked sections
snapshot the state a resolution derives from and commit its results - so helpers are reached
through the service and patching a service method keeps affecting every path.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
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


@dataclass(slots=True)
class _MappingSnapshot:
    """Registry state a mapping resolution was derived from.

    Resolution runs outside the registry lock, so committing its results has to prove the state
    they were built against has not changed meanwhile.
    """

    token: str | None
    depths: dict[str, int]
    mappings: dict[str, dict[str, str]]


def _snapshot_mappings(
    subscription_state: state._SubscriptionState, depths: dict[str, int] | None = None
) -> _MappingSnapshot:
    """Copy the mapping state under the lock; ``depths`` overrides the target depth set."""
    return _MappingSnapshot(
        token=subscription_state.mapping_model_token,
        depths=dict(subscription_state.monitored_objects) if depths is None else depths,
        mappings=dict(subscription_state.object_mappings),
    )


def _snapshot_matches(subscription_state: state._SubscriptionState, snapshot: _MappingSnapshot) -> bool:
    """Whether the registry state still matches what the pre-lock resolution derived from."""
    return (
        subscription_state.mapping_model_token == snapshot.token
        and subscription_state.monitored_objects == snapshot.depths
    )


def _resolve_mappings_for_depths(
    snapshot: _MappingSnapshot,
    target_depths: dict[str, int],
    model: BuildResult,
    source_index: dict[str, ModelNode],
    token: str,
    budget_nodes: int,
    budget_depth: int | None,
) -> dict[str, dict[str, str]]:
    """Prospective mappings for ``target_depths``, built into containers that are not yet committed.

    A refreshed model revision invalidates every mapping built against the previous revision, so
    nothing is reused; otherwise stored mappings survive for objects whose depth is unchanged.
    The node and composition-depth budgets are enforced here, so an oversized subtree is rejected
    before the registry lock is ever acquired.
    """
    stale_model = snapshot.token != token
    object_mappings: dict[str, dict[str, str]] = {}
    for element_id, depth in target_depths.items():
        stored = snapshot.mappings.get(element_id)
        if not stale_model and stored is not None and snapshot.depths.get(element_id) == depth:
            object_mappings[element_id] = stored
            continue
        object_mappings[element_id] = mapping._resolve_object_mappings(
            element_id,
            depth,
            model,
            source_index,
            max_nodes=budget_nodes,
            max_depth_limit=budget_depth,
        )
    return object_mappings


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
    token = model_revision(model)
    budget_nodes = service._subtree_max_nodes
    budget_depth = service._subtree_max_depth
    # The source index is a content cache keyed by the revision token, so it is safe to build
    # outside the lock; nothing in the resolution phase awaits.
    source_index = service._source_index(token, model)

    previous_monitored_nodes = 0
    monitored_nodes_count = 0
    monitored_objects_count = 0

    # Mapping resolution is CPU-bound (now bounded by visited nodes and the node/depth budgets),
    # so it runs outside the registry lock: holding the lock across a traversal stalls every other
    # subscription operation, and an address-space cycle used to wedge the event loop entirely.
    async with service._lock:
        subscription_state = service._subscriptions.get(subscription_id)
        if subscription_state is None or (client_id is not None and subscription_state.client_id != client_id):
            return False
        previous_monitored_nodes = len(subscription_state.monitored_node_ids)
        snapshot = _snapshot_mappings(subscription_state)

    target_depths = dict(snapshot.depths)
    target_depths.update({element_id: max_depth for element_id in element_ids})
    object_mappings = _resolve_mappings_for_depths(
        snapshot, target_depths, model, source_index, token, budget_nodes, budget_depth
    )

    async with service._lock:
        subscription_state = service._subscriptions.get(subscription_id)
        if subscription_state is None or (client_id is not None and subscription_state.client_id != client_id):
            return False
        if not _snapshot_matches(subscription_state, snapshot):
            # A concurrent register/unregister changed the state while the mappings were being
            # built. Re-resolve against the current state instead of committing stale results;
            # the traversal is bounded, so holding the lock here cannot wedge the loop.
            snapshot = _snapshot_mappings(subscription_state)
            target_depths = dict(snapshot.depths)
            target_depths.update({element_id: max_depth for element_id in element_ids})
            object_mappings = _resolve_mappings_for_depths(
                snapshot, target_depths, model, source_index, token, budget_nodes, budget_depth
            )
        # The budget counts the *union* of monitored nodes across the subscription: a rejected
        # registration must leave the previous monitored set intact, because truncating silently
        # would report monitoring that never happens upstream.
        monitored_node_ids, node_to_element_id = service._merge_object_mappings(object_mappings)
        if len(monitored_node_ids) > budget_nodes:
            raise AdmissionLimitError(
                400,
                "MonitoredItemBudget",
                "Monitored item budget exceeded",
                {
                    "subscriptionId": subscription_id,
                    "monitoredNodes": len(monitored_node_ids),
                    "maxNodes": budget_nodes,
                },
            )
        subscription_state.monitored_objects = target_depths
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
    token = model_revision(model)
    budget_nodes = service._subtree_max_nodes
    budget_depth = service._subtree_max_depth
    removed_ids = set(element_ids)
    source_index = service._source_index(token, model)

    previous_monitored_nodes = 0
    monitored_nodes_count = 0
    monitored_objects_count = 0

    def _remaining_depths(subscription_state: state._SubscriptionState) -> dict[str, int]:
        return {
            element_id: depth
            for element_id, depth in subscription_state.monitored_objects.items()
            if element_id not in removed_ids
        }

    # Same split as registration: the rebuild for a refreshed model revision is CPU-bound, so it
    # happens outside the lock and only the commit is guarded.
    async with service._lock:
        subscription_state = service._subscriptions.get(subscription_id)
        if subscription_state is None or (client_id is not None and subscription_state.client_id != client_id):
            return False
        previous_monitored_nodes = len(subscription_state.monitored_node_ids)
        snapshot = _snapshot_mappings(subscription_state, depths=_remaining_depths(subscription_state))

    object_mappings = _resolve_mappings_for_depths(
        snapshot, snapshot.depths, model, source_index, token, budget_nodes, budget_depth
    )

    async with service._lock:
        subscription_state = service._subscriptions.get(subscription_id)
        if subscription_state is None or (client_id is not None and subscription_state.client_id != client_id):
            return False
        if not _snapshot_matches(subscription_state, snapshot):
            # State changed while the mappings were being built: re-derive the remaining objects
            # against the current state (bounded, so holding the lock here cannot wedge the loop).
            snapshot = _snapshot_mappings(subscription_state, depths=_remaining_depths(subscription_state))
            object_mappings = _resolve_mappings_for_depths(
                snapshot, snapshot.depths, model, source_index, token, budget_nodes, budget_depth
            )
        monitored_node_ids, node_to_element_id = service._merge_object_mappings(object_mappings)
        subscription_state.monitored_objects = dict(snapshot.depths)
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
    max_depth_limit: int | None = None,
) -> tuple[set[str], dict[str, str]]:
    # ``max_nodes`` and ``max_depth_limit`` default to the service's subtree budgets.
    return mapping._resolve_monitored_node_ids(
        monitored_objects,
        model,
        max_nodes=service._subtree_max_nodes if max_nodes is None else max_nodes,
        max_depth_limit=service._subtree_max_depth if max_depth_limit is None else max_depth_limit,
    )


def _collect_property_source_mappings(
    service: SubscriptionService,
    model: BuildResult,
    root: ModelNode,
    max_depth: int,
    max_nodes: int | None = None,
    max_depth_limit: int | None = None,
) -> dict[str, str]:
    # ``max_nodes`` and ``max_depth_limit`` default to the service's subtree budgets.
    return mapping._collect_property_source_mappings(
        model,
        root,
        max_depth,
        max_nodes=service._subtree_max_nodes if max_nodes is None else max_nodes,
        max_depth_limit=service._subtree_max_depth if max_depth_limit is None else max_depth_limit,
    )
