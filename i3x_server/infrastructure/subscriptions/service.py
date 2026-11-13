"""Subscription service: i3X subscription state over shared OPC UA node monitors.

The shared vocabulary (updates, sync results, subscription/monitor/bin state)
lives in ``state.py``; the OPC UA monitoring runtime (native bin packing, the
shared polling loop, reconnect resync) lives in ``monitoring.py``; the
elementId-to-source-node mapping lives in ``mapping.py``. This module owns the
subscription-facing logic: lifecycle, CRUD, sync, SSE wake-up, and fan-out.
For private internals, tests and monkeypatching must target the source modules.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timezone
from math import ceil
from typing import Any
from uuid import uuid4

from i3x_server.domain.errors import AdmissionLimitError
from i3x_server.domain.ports.opcua import OpcUaClientProtocol, OpcUaSubscriptionCapabilities
from i3x_server.infrastructure.subscriptions import mapping, monitoring, state
from i3x_server.infrastructure.subscriptions.state import (
    SubscriptionDeleteResult,
    SubscriptionDetail,
    SubscriptionSyncResult,
    SubscriptionUpdate,
)
from i3x_server.model.revision import model_revision
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.opcua_value import decode_data_value, format_utc_timestamp, normalize_opcua_value
from i3x_server.schemas.state import BuildResult

logger = logging.getLogger(__name__)

__all__ = [
    "SubscriptionDeleteResult",
    "SubscriptionDetail",
    "SubscriptionService",
    "SubscriptionSyncResult",
    "SubscriptionUpdate",
]


class SubscriptionService:
    def __init__(
        self,
        opcua_client: OpcUaClientProtocol,
        interval_seconds: float,
        max_updates_per_subscription: int = 10000,
        ttl_seconds: int = 300,
        seed_initial_values: bool = True,
        native_timeout_refresh_mode: str = "adaptive",
        native_timeout_refresh_keepalives: int = 3,
        native_timeout_refresh_max_seconds: float = 30.0,
        max_concurrent_native_admissions: int = 4,
        native_backoff_seconds: float = 30.0,
        max_subscriptions: int = 1000,
        subtree_max_nodes: int = 5000,
        native_max_bins: int = 100,
        native_max_monitored_items: int = 10000,
    ) -> None:
        self._opcua_client = opcua_client
        self._interval_seconds = max(0.1, float(interval_seconds))
        self._max_updates_per_subscription = max(1, max_updates_per_subscription)
        self._ttl_seconds = max(1, ttl_seconds)
        self._seed_initial_values = seed_initial_values
        normalized_mode = native_timeout_refresh_mode.strip().lower()
        if normalized_mode not in {"hybrid", "strict", "adaptive"}:
            logger.warning(
                "Unknown native timeout refresh mode '%s'; falling back to hybrid",
                native_timeout_refresh_mode,
            )
            normalized_mode = "hybrid"
        self._native_timeout_refresh_mode = normalized_mode
        self._native_timeout_refresh_keepalives = max(1, int(native_timeout_refresh_keepalives))
        self._native_timeout_refresh_max_seconds = max(0.1, float(native_timeout_refresh_max_seconds))
        self._lock = asyncio.Lock()
        self._subscriptions: dict[str, state._SubscriptionState] = {}
        # Source-node lookup shared across subscriptions, rebuilt only when the model revision changes.
        self._source_index_cache: tuple[str, dict[str, ModelNode]] | None = None
        self._node_monitors: dict[str, state._NodeMonitor] = {}
        self._bins: dict[str, state._ManagedBin] = {}
        self._native_bin_admission_lock = asyncio.Lock()
        self._native_admission_semaphore = asyncio.Semaphore(max(1, int(max_concurrent_native_admissions)))
        self._native_backoff_seconds = max(0.1, float(native_backoff_seconds))
        self._native_backoff_until_monotonic: float = 0.0
        # Gateway-side admission limits. The upstream is the resource being protected: an
        # unbounded subscription count or subtree turns one request into thousands of monitored
        # items, so the caps reject instead of accepting and degrading every other client.
        self._max_subscriptions = max(1, int(max_subscriptions))
        self._subtree_max_nodes = max(1, int(subtree_max_nodes))
        self._native_max_bins = max(1, int(native_max_bins))
        self._native_max_monitored_items = max(1, int(native_max_monitored_items))
        self._cleanup_task: asyncio.Task[None] | None = None
        self._node_datachange_task: asyncio.Task[None] | None = None
        self._shared_polling_task: asyncio.Task[None] | None = None
        self._node_datachange_queue: asyncio.Queue[tuple[str, Any, state._ValueSignal]] = asyncio.Queue(
            maxsize=max(1000, self._max_updates_per_subscription)
        )
        # Per-node counts of updates dropped from the shared queue before fan-out; consumed
        # by ``_fanout_node_value`` as a sequence-gap marker on each affected subscription.
        self._dropped_node_datachanges: dict[str, int] = {}
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._shutdown_event: asyncio.Event = asyncio.Event()
        # Set by ``close()``: background tasks must never be resurrected afterwards, otherwise a
        # post-close operation (TTL sweep, datachange callback, reconnect) restarts them against an
        # already disconnecting client.
        self._closed = False
        self._reconnect_listener = self._handle_client_reconnect
        self._opcua_client.add_reconnect_listener(self._reconnect_listener)

    def _remember_event_loop(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._event_loop is None or self._event_loop.is_closed():
            self._event_loop = loop

    def _enqueue_node_datachange(self, node_id: str, value: Any, signal: state._ValueSignal = state._NO_SIGNAL) -> None:
        if self._node_datachange_queue.full():
            # Drop the oldest entry and remember it: silently discarding updates hides
            # gaps from consumers, so the loss is marked as a sequence gap on fan-out.
            try:
                dropped_node_id, _dropped_value, _dropped_signal = self._node_datachange_queue.get_nowait()
            except asyncio.QueueEmpty:
                dropped_node_id = None
            if dropped_node_id is not None:
                self._dropped_node_datachanges[dropped_node_id] = (
                    self._dropped_node_datachanges.get(dropped_node_id, 0) + 1
                )
        try:
            self._node_datachange_queue.put_nowait((node_id, value, signal))
        except asyncio.QueueFull:
            self._dropped_node_datachanges[node_id] = self._dropped_node_datachanges.get(node_id, 0) + 1

    async def _node_datachange_loop(self) -> None:
        try:
            while True:
                node_id, value, signal = await self._node_datachange_queue.get()
                try:
                    await self._fanout_node_value(node_id, value, signal)
                except Exception:
                    logger.exception(
                        "Shared node datachange fan-out failed",
                        extra={"node_id": node_id},
                    )
        except asyncio.CancelledError:
            return

    async def _fanout_node_value(self, node_id: str, value: Any, signal: state._ValueSignal = state._NO_SIGNAL) -> None:
        """Deliver one node-level value change to every subscription monitoring it."""
        # Normalise once for all subscribers: re-coercing the same value per subscription
        # (variant decode, container unpack) dominated the tick for wide fan-outs.
        normalized_value = normalize_opcua_value(value)
        # Registry lock: membership, mapping resolution, and the drop-count snapshot. The
        # per-subscription buffer then changes under each subscription's own lock, so ticks
        # for different subscriptions no longer serialize behind one global lock.
        async with self._lock:
            monitor = self._node_monitors.get(node_id)
            subscriber_ids = list(monitor.subscriber_ids) if monitor is not None else []
            dropped_count = self._dropped_node_datachanges.pop(node_id, 0)
            states: list[state._SubscriptionState] = []
            targets: list[tuple[state._SubscriptionState, str, str]] = []
            for subscription_id in subscriber_ids:
                subscription_state = self._subscriptions.get(subscription_id)
                if subscription_state is None:
                    continue
                states.append(subscription_state)
                resolved_node_id = self._resolve_target_node(subscription_state, node_id)
                if resolved_node_id is None:
                    continue
                element_id = self._resolve_element_id(subscription_state, resolved_node_id)
                targets.append((subscription_state, resolved_node_id, element_id))

        if dropped_count:
            # Advance the sequence by the number of updates lost on the shared queue so the
            # delivered update creates a visible gap; sync reports the range, and streams see
            # the jump.
            for subscription_state in states:
                async with subscription_state.lock:
                    if subscription_state.dropped_from_sequence is None:
                        subscription_state.dropped_from_sequence = subscription_state.sequence_number + 1
                    subscription_state.sequence_number += dropped_count
                    subscription_state.dropped_to_sequence = subscription_state.sequence_number

        for subscription_state, resolved_node_id, element_id in targets:
            async with subscription_state.lock:
                self._append_update(
                    subscription_state, resolved_node_id, normalized_value, signal, element_id=element_id
                )

    def schedule_node_datachange(self, node_id: str, value: Any, signal: state._ValueSignal = state._NO_SIGNAL) -> None:
        target_loop = self._event_loop
        if target_loop is not None and target_loop.is_closed():
            target_loop = None

        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if target_loop is None and current_loop is not None:
            self._event_loop = current_loop
            self._ensure_cleanup_task()
            self._enqueue_node_datachange(node_id, value, signal)
            return

        if current_loop is not None and target_loop is current_loop:
            self._ensure_cleanup_task()
            self._enqueue_node_datachange(node_id, value, signal)
            return

        if target_loop is None:
            logger.warning(
                "Dropping shared node datachange because no running event loop is available",
                extra={"node_id": node_id},
            )
            return

        target_loop.call_soon_threadsafe(self._ensure_cleanup_task)
        target_loop.call_soon_threadsafe(self._enqueue_node_datachange, node_id, value, signal)

    def _now_monotonic(self) -> float:
        self._remember_event_loop()
        return asyncio.get_running_loop().time()

    def _touch(self, state_obj: state._SubscriptionState) -> None:
        state_obj.last_activity_monotonic = self._now_monotonic()

    def _ensure_cleanup_task(self) -> None:
        if self._closed:
            # ``close()`` already tore the runtime down; a late call (TTL sweep, datachange callback,
            # reconnect, sync after shutdown) must not resurrect background tasks.
            return
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        if self._node_datachange_task is None or self._node_datachange_task.done():
            self._node_datachange_task = asyncio.create_task(self._node_datachange_loop())
        if self._shared_polling_task is None or self._shared_polling_task.done():
            self._shared_polling_task = asyncio.create_task(monitoring._shared_polling_loop(self))

    async def _cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(max(0.1, self._interval_seconds))
                await self._expire_subscriptions()
        except asyncio.CancelledError:
            return

    async def _expire_subscriptions(self) -> None:
        # Keep subscription references out of the long-lived cleanup loop's frame.
        stale_ids: list[str] = []
        now = self._now_monotonic()
        async with self._lock:
            for subscription_state in self._subscriptions.values():
                if now - subscription_state.last_activity_monotonic >= self._ttl_seconds:
                    stale_ids.append(subscription_state.subscription_id)

            removed: list[state._SubscriptionState] = []
            for subscription_id in stale_ids:
                if subscription_id not in self._subscriptions:
                    continue
                subscription_state = self._subscriptions.pop(subscription_id)
                subscription_state.update_event.set()
                removed.append(subscription_state)

        for subscription_state in removed:
            logger.warning("Subscription expired by TTL subscription_id=%s", subscription_state.subscription_id)
            await self._release_all_nodes(subscription_state)

    async def _handle_client_reconnect(self) -> None:
        # Native-bin resync lives in ``monitoring._handle_client_reconnect``; this stays the
        # listener entry point registered on the OPC UA client.
        await monitoring._handle_client_reconnect(self)

    def initiate_shutdown(self) -> None:
        """Signal all active SSE streams to close immediately.

        Call this as early as possible (e.g. from a SIGINT/SIGTERM handler)
        so that streaming HTTP connections are released *before* Uvicorn
        enters its "Waiting for connections to close" phase.  The full
        ``close()`` coroutine still needs to be awaited afterwards to stop
        OPC UA subscriptions and disconnect the client.
        """
        self._shutdown_event.set()
        # Also pulse every per-subscription event so that tasks blocked in
        # wait_for_updates() wake up immediately without waiting for their
        # next timeout cycle.
        for subscription_state in self._subscriptions.values():
            subscription_state.update_event.set()

    async def close(self) -> None:
        # Signal all waiting SSE stream tasks to wake up and terminate *before*
        # any cleanup so that HTTP connections can be closed while Uvicorn is
        # still in its "Waiting for connections to close" phase.
        self._shutdown_event.set()
        self._closed = True
        async with self._lock:
            subscriptions = list(self._subscriptions.values())

        for subscription in subscriptions:
            # Signal any waiting stream tasks (wait_for_updates) to wake up and terminate
            subscription.update_event.set()
            await self._release_all_nodes(subscription)

        async with self._lock:
            self._subscriptions.clear()

        cleanup_task = self._cleanup_task
        self._cleanup_task = None
        if cleanup_task is not None:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass

        node_datachange_task = self._node_datachange_task
        self._node_datachange_task = None
        if node_datachange_task is not None:
            node_datachange_task.cancel()
            try:
                await node_datachange_task
            except asyncio.CancelledError:
                pass

        shared_polling_task = self._shared_polling_task
        self._shared_polling_task = None
        if shared_polling_task is not None:
            shared_polling_task.cancel()
            try:
                await shared_polling_task
            except asyncio.CancelledError:
                pass

        remove_listener = getattr(self._opcua_client, "remove_reconnect_listener", None)
        if callable(remove_listener):
            try:
                remove_listener(self._reconnect_listener)
            except Exception:
                logger.debug("Failed to remove reconnect listener", exc_info=True)

    async def create_subscription(self, client_id: str | None, display_name: str | None = None) -> SubscriptionDetail:
        self._ensure_cleanup_task()
        subscription_id = f"sub-{uuid4()}"
        subscription_state = state._SubscriptionState(
            subscription_id=subscription_id,
            client_id=client_id,
            display_name=display_name,
            monitored_objects={},
            last_activity_monotonic=self._now_monotonic(),
        )
        async with self._lock:
            active_count = len(self._subscriptions)
            if active_count >= self._max_subscriptions:
                # Rejected before insertion: a partially created subscription would leak state
                # and count towards the cap it just exceeded.
                raise AdmissionLimitError(
                    429,
                    "SubscriptionLimit",
                    "Subscription limit reached",
                    {"maxSubscriptions": self._max_subscriptions, "activeSubscriptions": active_count},
                )
            self._subscriptions[subscription_id] = subscription_state
            # Snapshot inside the lock: ``register_items`` mutates ``state.monitored_objects`` and a
            # detail built after release can race with it ("dictionary changed size during iteration").
            detail = self._to_detail(subscription_state)
        logger.info(
            "Subscription created subscription_id=%s client_id=%s display_name=%s",
            subscription_id,
            client_id,
            display_name,
        )
        return detail

    async def list_subscriptions(
        self,
        client_id: str | None,
        subscription_ids: list[str] | None = None,
    ) -> list[SubscriptionDetail]:
        async with self._lock:
            if client_id is None:
                values = list(self._subscriptions.values())
            else:
                values = [item for item in self._subscriptions.values() if item.client_id == client_id]
            if subscription_ids is not None:
                allowed = set(subscription_ids)
                values = [item for item in values if item.subscription_id in allowed]
            # Snapshot inside the lock for the same reason as ``create_subscription``.
            return [self._to_detail(item) for item in values]

    async def delete_subscriptions(
        self,
        client_id: str | None,
        subscription_ids: list[str],
    ) -> list[SubscriptionDeleteResult]:
        removed: list[state._SubscriptionState] = []
        results: list[SubscriptionDeleteResult] = []

        async with self._lock:
            for subscription_id in subscription_ids:
                subscription_state = self._subscriptions.get(subscription_id)
                if subscription_state is None:
                    results.append(
                        SubscriptionDeleteResult(
                            success=False,
                            subscription_id=subscription_id,
                            error={"code": 404, "message": "Subscription not found"},
                        )
                    )
                    continue
                if client_id is not None and subscription_state.client_id != client_id:
                    results.append(
                        SubscriptionDeleteResult(
                            success=False,
                            subscription_id=subscription_id,
                            error={"code": 404, "message": "Subscription not found"},
                        )
                    )
                    continue
                self._subscriptions.pop(subscription_id, None)
                removed.append(subscription_state)
                subscription_state.update_event.set()
                results.append(SubscriptionDeleteResult(success=True, subscription_id=subscription_id))

        for subscription_state in removed:
            await self._release_all_nodes(subscription_state)
            logger.info(
                "Subscription deleted subscription_id=%s client_id=%s monitored_nodes=%d",
                subscription_state.subscription_id,
                subscription_state.client_id,
                len(subscription_state.monitored_node_ids),
            )

        return results

    async def register_items(
        self,
        client_id: str | None,
        subscription_id: str,
        element_ids: list[str],
        max_depth: int,
        model: BuildResult,
    ) -> bool:
        self._ensure_cleanup_task()
        requested_count = len(element_ids)
        previous_monitored_nodes = 0
        monitored_nodes_count = 0
        monitored_objects_count = 0
        async with self._lock:
            subscription_state = self._subscriptions.get(subscription_id)
            if subscription_state is None or (client_id is not None and subscription_state.client_id != client_id):
                return False
            previous_node_ids = set(subscription_state.monitored_node_ids)
            previous_monitored_nodes = len(previous_node_ids)
            token = model_revision(model)
            source_index = self._source_index(token, model)
            budget = self._subtree_max_nodes
            # Mappings are built into prospective containers and committed only after the budget
            # check. A rejected registration must leave the previous monitored set intact -
            # truncating silently would report monitoring that never happens upstream.
            depths = dict(subscription_state.monitored_objects)
            if subscription_state.mapping_model_token != token:
                # A refreshed model invalidates every mapping built against the previous revision.
                depths.update({element_id: max_depth for element_id in element_ids})
                object_mappings = {
                    element_id: mapping._resolve_object_mappings(
                        element_id, depth, model, source_index, max_nodes=budget
                    )
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
            monitored_node_ids, node_to_element_id = self._merge_object_mappings(object_mappings)
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
            self._touch(subscription_state)

        await self._apply_monitored_node_diff(subscription_id)
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

    async def unregister_items(
        self,
        client_id: str | None,
        subscription_id: str,
        element_ids: list[str],
        model: BuildResult,
    ) -> bool:
        self._ensure_cleanup_task()
        requested_count = len(element_ids)
        previous_monitored_nodes = 0
        monitored_nodes_count = 0
        monitored_objects_count = 0
        async with self._lock:
            subscription_state = self._subscriptions.get(subscription_id)
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
                source_index = self._source_index(token, model)
                subscription_state.object_mappings = {
                    element_id: mapping._resolve_object_mappings(element_id, depth, model, source_index)
                    for element_id, depth in subscription_state.monitored_objects.items()
                }
            else:
                for element_id in element_ids:
                    subscription_state.object_mappings.pop(element_id, None)
            monitored_node_ids, node_to_element_id = self._merge_object_mappings(subscription_state.object_mappings)
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
            self._touch(subscription_state)

        await self._apply_monitored_node_diff(subscription_id)
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

    async def sync(
        self,
        client_id: str | None,
        subscription_id: str,
        acknowledge_sequence: int | None = None,
        allow_when_stream_active: bool = False,
    ) -> SubscriptionSyncResult | None:
        self._ensure_cleanup_task()
        should_refresh = False
        async with self._lock:
            subscription_state = self._subscriptions.get(subscription_id)
            if subscription_state is None or (client_id is not None and subscription_state.client_id != client_id):
                return None

            if subscription_state.stream_connected and not allow_when_stream_active:
                # Keep the TTL refreshed: a client polling while a stream is open is still active,
                # otherwise the subscription expires mid-stream (400 "stream is active" until TTL).
                # Report the overflow markers without consuming them: polling can see dropped
                # updates while the stream delivers a silent sequence jump.
                self._touch(subscription_state)
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

            self._touch(subscription_state)

        if should_refresh:
            await self._refresh_changed_values(subscription_id)

        async with self._lock:
            subscription_state = self._subscriptions.get(subscription_id)
            if subscription_state is None or (client_id is not None and subscription_state.client_id != client_id):
                return None
            self._touch(subscription_state)
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

    async def activate_stream(self, client_id: str | None, subscription_id: str) -> int | None:
        self._ensure_cleanup_task()
        async with self._lock:
            subscription_state = self._subscriptions.get(subscription_id)
            if subscription_state is None or (client_id is not None and subscription_state.client_id != client_id):
                return None
            subscription_state.active_stream_generation += 1
            subscription_state.stream_connected = True
            self._touch(subscription_state)
            subscription_state.update_event.set()
            if state._stream_debug_enabled():
                logger.info(
                    "Subscription service activate stream subscription_id=%s generation=%s monitored_nodes=%s mode=%s",
                    subscription_id,
                    subscription_state.active_stream_generation,
                    len(subscription_state.monitored_node_ids),
                    subscription_state.mode,
                )
            return subscription_state.active_stream_generation

    async def deactivate_stream(self, subscription_id: str, stream_generation: int) -> None:
        async with self._lock:
            subscription_state = self._subscriptions.get(subscription_id)
            if subscription_state is None:
                return
            if subscription_state.active_stream_generation == stream_generation:
                subscription_state.stream_connected = False
                self._touch(subscription_state)
                if state._stream_debug_enabled():
                    logger.info(
                        "Subscription service deactivate stream subscription_id=%s generation=%s",
                        subscription_id,
                        stream_generation,
                    )

    async def is_stream_active(self, subscription_id: str, stream_generation: int) -> bool:
        async with self._lock:
            subscription_state = self._subscriptions.get(subscription_id)
            if subscription_state is None:
                return False
            return subscription_state.active_stream_generation == stream_generation

    async def wait_for_updates(
        self,
        client_id: str | None,
        subscription_id: str,
        after_sequence: int,
        timeout_seconds: float = 15.0,
    ) -> list[SubscriptionUpdate] | None:
        self._ensure_cleanup_task()
        should_refresh_after_timeout = False
        refresh_interval_seconds: float | None = None
        refresh_timeout_threshold = 1
        async with self._lock:
            subscription_state = self._subscriptions.get(subscription_id)
            if subscription_state is None or (client_id is not None and subscription_state.client_id != client_id):
                return None

            self._touch(subscription_state)

            async with subscription_state.lock:
                current = self._updates_after(subscription_state, after_sequence)
                if current:
                    subscription_state.native_timeout_count = 0
                    return current

                subscription_state.update_event.clear()
                wait_event = subscription_state.update_event

            should_refresh_after_timeout = (
                subscription_state.mode == "native"
                and bool(subscription_state.monitored_node_ids)
                and self._native_timeout_refresh_mode != "strict"
            )
            refresh_interval_seconds = subscription_state.native_refresh_interval_seconds
            if should_refresh_after_timeout and self._native_timeout_refresh_mode == "adaptive":
                interval_seconds = refresh_interval_seconds or self._interval_seconds
                refresh_timeout_threshold = max(1, ceil(interval_seconds / max(0.001, timeout_seconds)))

        # Race: per-subscription update, service-wide shutdown, or wall-clock timeout.
        # Using asyncio.wait so we can also abort early on server shutdown without
        # replacing Uvicorn's own SIGINT/SIGTERM handling.
        _wakeup = asyncio.ensure_future(wait_event.wait())
        _abort = asyncio.ensure_future(self._shutdown_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {_wakeup, _abort},
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            # ``asyncio.wait`` does not cancel the futures it leaves pending - and it also does not
            # cancel them when *this* coroutine is cancelled (client disconnect, shutdown). Both
            # futures must be cancelled and awaited here, otherwise every SSE stream leaks two
            # orphaned tasks ("Task was destroyed but it is pending").
            _wakeup.cancel()
            _abort.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.gather(_wakeup, _abort, return_exceptions=True)

        if self._shutdown_event.is_set():
            # Server is shutting down; tell the SSE generator to close gracefully.
            return None

        if not done:  # wall-clock timeout – same logic as the former except TimeoutError block
            should_execute_refresh = False
            timeout_count = 0
            async with self._lock:
                subscription_state = self._subscriptions.get(subscription_id)
                if subscription_state is None or (client_id is not None and subscription_state.client_id != client_id):
                    return None
                if should_refresh_after_timeout:
                    async with subscription_state.lock:
                        subscription_state.native_timeout_count += 1
                        timeout_count = subscription_state.native_timeout_count
                    if self._native_timeout_refresh_mode == "adaptive":
                        should_execute_refresh = timeout_count >= refresh_timeout_threshold
                    else:
                        should_execute_refresh = True

            if should_refresh_after_timeout:
                if state._stream_debug_enabled() and self._native_timeout_refresh_mode == "adaptive":
                    logger.info(
                        "Subscription service wait timeout native subscription_id=%s after_sequence=%s "
                        "timeout_count=%d threshold=%d refresh_interval_s=%.3f",
                        subscription_id,
                        after_sequence,
                        timeout_count,
                        refresh_timeout_threshold,
                        refresh_interval_seconds or self._interval_seconds,
                    )
                if should_execute_refresh:
                    async with self._lock:
                        subscription_state = self._subscriptions.get(subscription_id)
                        if subscription_state is None or (
                            client_id is not None and subscription_state.client_id != client_id
                        ):
                            return None
                        async with subscription_state.lock:
                            subscription_state.native_timeout_count = 0
                if state._stream_debug_enabled() and should_execute_refresh:
                    logger.info(
                        "Subscription service wait timeout refresh subscription_id=%s after_sequence=%s",
                        subscription_id,
                        after_sequence,
                    )
                if should_execute_refresh:
                    await self._refresh_changed_values(subscription_id)
            async with self._lock:
                subscription_state = self._subscriptions.get(subscription_id)
                if subscription_state is None or (client_id is not None and subscription_state.client_id != client_id):
                    return None
                self._touch(subscription_state)
                async with subscription_state.lock:
                    updates = self._updates_after(subscription_state, after_sequence)
                if state._stream_debug_enabled():
                    logger.info(
                        "Subscription service wait timeout result subscription_id=%s updates=%s after_sequence=%s",
                        subscription_id,
                        len(updates),
                        after_sequence,
                    )
                return updates

        async with self._lock:
            subscription_state = self._subscriptions.get(subscription_id)
            if subscription_state is None or (client_id is not None and subscription_state.client_id != client_id):
                return None
            self._touch(subscription_state)
            async with subscription_state.lock:
                subscription_state.native_timeout_count = 0
                return self._updates_after(subscription_state, after_sequence)

    async def handle_datachange(
        self,
        subscription_id: str,
        node_id: str,
        value: Any,
        signal: state._ValueSignal = state._NO_SIGNAL,
    ) -> None:
        # Normalise before taking the lock: the value coercion (variant decode,
        # container unpack) is the expensive part and must not serialise other
        # subscriptions behind it.
        normalized_value = normalize_opcua_value(value)
        async with self._lock:
            subscription_state = self._subscriptions.get(subscription_id)
            if subscription_state is None:
                return

            resolved_node_id = self._resolve_target_node(subscription_state, node_id)
            if resolved_node_id is None:
                return
            # Mapping lookup stays under the registry lock; the buffer append then runs under
            # this subscription's own lock, so deliveries for other subscriptions are not
            # blocked behind it.
            element_id = self._resolve_element_id(subscription_state, resolved_node_id)
            logger.debug(
                "Subscription datachange callback subscription_id=%s mode=%s node_id=%s",
                subscription_id,
                subscription_state.mode,
                resolved_node_id,
            )
            # Removed self._touch(state) to ensure server notifications don't keep stale subscriptions alive.
            # Only client activity (sync, wait_for_updates) should refresh the TTL.

        async with subscription_state.lock:
            self._append_update(subscription_state, resolved_node_id, normalized_value, signal, element_id=element_id)
            subscription_state.native_timeout_count = 0

    async def _apply_monitored_node_diff(self, subscription_id: str) -> None:
        """Reconcile the shared node registry against the subscription's *current* desired set.

        Recomputed under the lock from ``applied_node_ids`` (what is actually monitored), so a
        concurrent register/unregister cannot leave an acquired node that the final desired set
        no longer contains. Network calls happen outside the lock.
        """
        async with self._lock:
            subscription_state = self._subscriptions.get(subscription_id)
            if subscription_state is None:
                return
            desired = set(subscription_state.monitored_node_ids)
            removed = subscription_state.applied_node_ids - desired
            added = desired - subscription_state.applied_node_ids
            subscription_state.applied_node_ids = desired

        for node_id in removed:
            await self._release_node_monitor(node_id, subscription_id)
        if added:
            # Concurrent admissions: bin selection stays serialized (packing), the OPC UA
            # round-trips run under the native admission semaphore, same as cross-subscription
            # registrations already do.
            await asyncio.gather(*(self._acquire_node_monitor(node_id, subscription_id) for node_id in sorted(added)))

        async with self._lock:
            subscription_state = self._subscriptions.get(subscription_id)
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
                await self._release_node_monitor(node_id, subscription_id)
            return

        if orphaned:
            await asyncio.gather(
                *(self._release_node_monitor(node_id, subscription_id) for node_id in sorted(orphaned))
            )

        await self._update_subscription_mode(subscription_id)
        await self._seed_initial_updates(subscription_id)

    async def _release_all_nodes(self, subscription_state: state._SubscriptionState) -> None:
        # Release everything that was ever acquired, not just the final desired set.
        for node_id in list(subscription_state.applied_node_ids | subscription_state.monitored_node_ids):
            await self._release_node_monitor(node_id, subscription_state.subscription_id)
        async with self._lock:
            subscription_state.mode = "idle"
            subscription_state.native_refresh_interval_seconds = None

    async def _update_subscription_mode(self, subscription_id: str) -> None:
        async with self._lock:
            subscription_state = self._subscriptions.get(subscription_id)
            if subscription_state is None:
                return
            if not subscription_state.monitored_node_ids:
                subscription_state.mode = "idle"
                subscription_state.native_refresh_interval_seconds = None
                return

            modes: list[str] = []
            native_intervals: list[float] = []
            for node_id in subscription_state.monitored_node_ids:
                monitor = self._node_monitors.get(node_id)
                mode = monitor.mode if monitor is not None else "idle"
                modes.append(mode)
                if (
                    monitor is not None
                    and monitor.mode == "native"
                    and monitor.native_refresh_interval_seconds is not None
                ):
                    native_intervals.append(monitor.native_refresh_interval_seconds)

            if modes and all(mode == "native" for mode in modes):
                subscription_state.mode = "native"
                subscription_state.native_refresh_interval_seconds = min(native_intervals) if native_intervals else None
            else:
                subscription_state.mode = "polling"
                subscription_state.native_refresh_interval_seconds = None

    async def _acquire_node_monitor(self, node_id: str, subscription_id: str) -> None:
        async with self._lock:
            monitor = self._node_monitors.get(node_id)
            if monitor is not None:
                monitor.subscriber_ids.add(subscription_id)
                return
            monitor = state._NodeMonitor(node_id=node_id)
            monitor.subscriber_ids.add(subscription_id)
            self._node_monitors[node_id] = monitor

        await monitoring._start_node_monitor_runtime(self, monitor)

    async def _release_node_monitor(self, node_id: str, subscription_id: str) -> None:
        async with self._lock:
            monitor = self._node_monitors.get(node_id)
            if monitor is None:
                return
            monitor.subscriber_ids.discard(subscription_id)
            should_stop = not monitor.subscriber_ids
            if should_stop:
                del self._node_monitors[node_id]
                # Nothing will deliver this node anymore: queued drop counts would fabricate a
                # sequence gap no consumer can observe and leak the entry forever.
                self._dropped_node_datachanges.pop(node_id, None)

        if should_stop:
            await monitoring._stop_node_monitor_runtime(self, monitor)

    def _can_open_new_bin(self, caps: OpcUaSubscriptionCapabilities) -> bool:
        # Kept as the service-facing entry point; the budget logic lives in ``monitoring``.
        return monitoring._can_open_new_bin(self, caps)

    async def _add_node_to_bin(self, monitor: state._NodeMonitor, target_bin: state._ManagedBin) -> bool:
        # Kept as the service-facing entry point; the admission round-trip lives in ``monitoring``.
        return await monitoring._add_node_to_bin(self, monitor, target_bin)

    async def _shared_polling_loop(self) -> None:
        # Kept as the service-facing entry point; the batched read loop lives in ``monitoring``.
        await monitoring._shared_polling_loop(self)

    async def _seed_initial_updates(self, subscription_id: str) -> None:
        if not self._seed_initial_values:
            return

        await self._refresh_changed_values(subscription_id)

    async def _refresh_changed_values(self, subscription_id: str) -> None:
        node_ids: list[str] = []

        async with self._lock:
            subscription_state = self._subscriptions.get(subscription_id)
            if subscription_state is None or subscription_state.mode == "idle":
                return
            node_ids = sorted(subscription_state.monitored_node_ids)

        if not node_ids:
            return

        try:
            data_values = await self._opcua_client.read_data_values(node_ids)
            if len(data_values) != len(node_ids):
                raise RuntimeError(f"Read returned {len(data_values)} values for {len(node_ids)} monitored nodes")
        except Exception:
            logger.exception("Initial subscription snapshot failed", extra={"subscription_id": subscription_id})
            return

        decoded: list[tuple[str, Any, state._ValueSignal]] = []
        for node_id, data_value in zip(node_ids, data_values, strict=True):
            payload, quality, timestamp = decode_data_value(data_value)
            decoded.append((node_id, normalize_opcua_value(payload), state._ValueSignal(quality, timestamp)))

        async with self._lock:
            subscription_state = self._subscriptions.get(subscription_id)
            if subscription_state is None:
                return
            delivered = [
                (node_id, payload, signal, self._resolve_element_id(subscription_state, node_id))
                for node_id, payload, signal in decoded
                if node_id in subscription_state.monitored_node_ids
            ]
            self._touch(subscription_state)

        async with subscription_state.lock:
            for node_id, payload, signal, element_id in delivered:
                self._append_update(subscription_state, node_id, payload, signal, element_id=element_id)

    def _resolve_target_node(self, subscription_state: state._SubscriptionState, node_id: str) -> str | None:
        """Resolve the monitored node an incoming notification refers to.

        Must be called under ``self._lock``: ``monitored_node_ids`` and ``node_to_element_id``
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

    def _resolve_element_id(self, subscription_state: state._SubscriptionState, node_id: str) -> str:
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

    @staticmethod
    def _updates_after(subscription_state: state._SubscriptionState, after_sequence: int) -> list[SubscriptionUpdate]:
        """Buffered updates with a sequence number greater than ``after_sequence``.

        Sequences strictly increase along the buffer, so collecting backwards from the tail
        stops at the first entry the consumer already acknowledged: O(delivered) instead of
        O(buffer). A deque cannot be sliced, so the walk appends and reverses.
        """
        updates = subscription_state.updates
        delivered: list[SubscriptionUpdate] = []
        for index in range(len(updates) - 1, -1, -1):
            item = updates[index]
            if int(item.sequence_number) <= after_sequence:
                break
            delivered.append(item)
        delivered.reverse()
        return delivered

    def _append_update(
        self,
        subscription_state: state._SubscriptionState,
        node_id: str,
        value: Any,
        signal: state._ValueSignal = state._NO_SIGNAL,
        element_id: str | None = None,
    ) -> None:
        """Append one update to a subscription's buffer.

        Callers hold ``subscription_state.lock`` (buffer, sequence counter, and drop markers are
        guarded there); ``element_id`` is resolved by the caller under the registry lock. It
        stays optional for legacy callers, which resolve the mapping inline.
        """
        # `value` is expected to be normalised already by the caller (outside the lock).
        quality = signal.quality or ("GoodNoData" if value is None else "Good")
        if value is None and quality in ("Good", "Uncertain"):
            quality = "GoodNoData"

        has_current = node_id in subscription_state.last_values_by_node_id
        current = subscription_state.last_values_by_node_id.get(node_id)
        current_quality = subscription_state.last_quality_by_node_id.get(node_id)
        if has_current and current == value and current_quality == quality:
            return

        subscription_state.last_values_by_node_id[node_id] = value
        subscription_state.last_quality_by_node_id[node_id] = quality
        subscription_state.sequence_number += 1
        if element_id is None:
            element_id = self._resolve_element_id(subscription_state, node_id)
        if len(subscription_state.updates) >= self._max_updates_per_subscription:
            # O(1) eviction; ``list.pop(0)`` re-shifted every remaining entry per update,
            # so a saturated subscription rewrote its whole buffer on each tick.
            dropped = subscription_state.updates.popleft()
            if subscription_state.dropped_from_sequence is None:
                subscription_state.dropped_from_sequence = dropped.sequence_number
            subscription_state.dropped_to_sequence = dropped.sequence_number
        subscription_state.updates.append(
            SubscriptionUpdate(
                sequence_number=subscription_state.sequence_number,
                element_id=element_id,
                node_id=node_id,
                value=value,
                quality=quality,
                timestamp=signal.timestamp or format_utc_timestamp(datetime.now(timezone.utc)),
            )
        )
        subscription_state.update_event.set()

    def _to_detail(self, subscription_state: state._SubscriptionState) -> SubscriptionDetail:
        monitored = [
            {"elementId": element_id, "maxDepth": max_depth}
            for element_id, max_depth in subscription_state.monitored_objects.items()
        ]
        return SubscriptionDetail(
            subscription_id=subscription_state.subscription_id,
            client_id=subscription_state.client_id,
            display_name=subscription_state.display_name,
            monitored_objects=monitored,
            mode=subscription_state.mode,
        )

    def _source_index(self, token: str, model: BuildResult) -> dict[str, ModelNode]:
        """Model source-node lookup cached per model revision token."""
        cached = self._source_index_cache
        if cached is None or cached[0] != token:
            cached = (token, mapping._build_source_index(model))
            self._source_index_cache = cached
        return cached[1]

    @staticmethod
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
        self,
        monitored_objects: dict[str, int],
        model: BuildResult,
        max_nodes: int | None = None,
    ) -> tuple[set[str], dict[str, str]]:
        # Kept as the service-facing entry point; the mapping lives in ``mapping.py``.
        # ``max_nodes`` defaults to the service's subtree budget.
        return mapping._resolve_monitored_node_ids(
            monitored_objects, model, max_nodes=self._subtree_max_nodes if max_nodes is None else max_nodes
        )

    def _collect_property_source_mappings(
        self,
        model: BuildResult,
        root: ModelNode,
        max_depth: int,
        max_nodes: int | None = None,
    ) -> dict[str, str]:
        # Kept as the service-facing entry point; the mapping lives in ``mapping.py``.
        # ``max_nodes`` defaults to the service's subtree budget.
        return mapping._collect_property_source_mappings(
            model, root, max_depth, max_nodes=self._subtree_max_nodes if max_nodes is None else max_nodes
        )
