"""Shared node datachange fan-out and per-subscription update buffering.

The queue decouples OPC UA callbacks from the asyncio loop that serves requests; the
buffer primitives are the only place a subscription's sequence counter, drop markers,
and last-value maps change.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from i3x_server.infrastructure.subscriptions import state
from i3x_server.infrastructure.subscriptions.state import SubscriptionUpdate
from i3x_server.schemas.opcua_value import normalize_opcua_value, now_iso, quality_for_payload

if TYPE_CHECKING:
    from i3x_server.infrastructure.subscriptions.service import SubscriptionService

logger = logging.getLogger(__name__)

# Queue marker for a notification dropped at the adapter boundary (its node id could not be
# resolved). The entry carries the bin id in place of a value: the notification came from a
# shared native subscription, so every subscription attached to that bin may have lost it.
_NOTIFICATION_GAP_NODE_ID = "__notification-gap__"


def _count_dropped_entry(service: SubscriptionService, node_id: str, value: Any) -> None:
    """Record an entry evicted from the shared queue as a drop on its own consumer path."""
    if node_id == _NOTIFICATION_GAP_NODE_ID:
        bin_id = str(value)
        service._dropped_notification_gaps[bin_id] = service._dropped_notification_gaps.get(bin_id, 0) + 1
        return
    service._dropped_node_datachanges[node_id] = service._dropped_node_datachanges.get(node_id, 0) + 1


def _enqueue_node_datachange(
    service: SubscriptionService,
    node_id: str,
    value: Any,
    signal: state._ValueSignal = state._NO_SIGNAL,
) -> None:
    if service._node_datachange_queue.full():
        # Drop the oldest entry and remember it: silently discarding updates hides
        # gaps from consumers, so the loss is marked as a sequence gap on fan-out.
        try:
            dropped_node_id, dropped_value, _dropped_signal = service._node_datachange_queue.get_nowait()
        except asyncio.QueueEmpty:
            dropped_node_id = None
        if dropped_node_id is not None:
            _count_dropped_entry(service, dropped_node_id, dropped_value)
    try:
        service._node_datachange_queue.put_nowait((node_id, value, signal))
    except asyncio.QueueFull:
        _count_dropped_entry(service, node_id, value)


def _enqueue_notification_gap(service: SubscriptionService, bin_id: str) -> None:
    """Queue a sequence gap for ``bin_id``. Room is made like a datachange entry: a gap
    must never be the entry the overflow discards."""
    if service._node_datachange_queue.full():
        try:
            dropped_node_id, dropped_value, _dropped_signal = service._node_datachange_queue.get_nowait()
        except asyncio.QueueEmpty:
            dropped_node_id = None
        if dropped_node_id is not None:
            _count_dropped_entry(service, dropped_node_id, dropped_value)
    try:
        service._node_datachange_queue.put_nowait((_NOTIFICATION_GAP_NODE_ID, bin_id, state._NO_SIGNAL))
    except asyncio.QueueFull:
        service._dropped_notification_gaps[bin_id] = service._dropped_notification_gaps.get(bin_id, 0) + 1


async def _node_datachange_loop(service: SubscriptionService) -> None:
    try:
        while True:
            node_id, value, signal = await service._node_datachange_queue.get()
            if node_id == _NOTIFICATION_GAP_NODE_ID:
                try:
                    await service._record_notification_gap(str(value))
                except Exception:
                    logger.exception(
                        "Notification gap recording failed",
                        extra={"bin_id": value},
                    )
                continue
            try:
                await service._fanout_node_value(node_id, value, signal)
            except Exception:
                logger.exception(
                    "Shared node datachange fan-out failed",
                    extra={"node_id": node_id},
                )
    except asyncio.CancelledError:
        return


async def _fanout_node_value(
    service: SubscriptionService,
    node_id: str,
    value: Any,
    signal: state._ValueSignal = state._NO_SIGNAL,
) -> None:
    """Deliver one node-level value change to every subscription monitoring it."""
    # Normalise once for all subscribers: re-coercing the same value per subscription
    # (variant decode, container unpack) dominated the tick for wide fan-outs.
    normalized_value = normalize_opcua_value(value)
    # Registry lock: membership, mapping resolution, and the drop-count snapshot. The
    # per-subscription buffer then changes under each subscription's own lock, so ticks
    # for different subscriptions no longer serialize behind one global lock.
    async with service._lock:
        monitor = service._node_monitors.get(node_id)
        subscriber_ids = list(monitor.subscriber_ids) if monitor is not None else []
        dropped_count = service._dropped_node_datachanges.pop(node_id, 0)
        states: list[state._SubscriptionState] = []
        targets: list[tuple[state._SubscriptionState, str, str]] = []
        for subscription_id in subscriber_ids:
            subscription_state = service._subscriptions.get(subscription_id)
            if subscription_state is None:
                continue
            states.append(subscription_state)
            resolved_node_id = service._resolve_target_node(subscription_state, node_id)
            if resolved_node_id is None:
                continue
            element_id = service._resolve_element_id(subscription_state, resolved_node_id)
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
            service._append_update(
                subscription_state, resolved_node_id, normalized_value, signal, element_id=element_id
            )


def schedule_node_datachange(
    service: SubscriptionService,
    node_id: str,
    value: Any,
    signal: state._ValueSignal = state._NO_SIGNAL,
) -> None:
    target_loop = service._event_loop
    if target_loop is not None and target_loop.is_closed():
        target_loop = None

    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if target_loop is None and current_loop is not None:
        service._event_loop = current_loop
        service._ensure_cleanup_task()
        service._enqueue_node_datachange(node_id, value, signal)
        return

    if current_loop is not None and target_loop is current_loop:
        service._ensure_cleanup_task()
        service._enqueue_node_datachange(node_id, value, signal)
        return

    if target_loop is None:
        logger.warning(
            "Dropping shared node datachange because no running event loop is available",
            extra={"node_id": node_id},
        )
        return

    target_loop.call_soon_threadsafe(service._ensure_cleanup_task)
    target_loop.call_soon_threadsafe(service._enqueue_node_datachange, node_id, value, signal)


def schedule_notification_gap(service: SubscriptionService, bin_id: str) -> None:
    """Schedule the sequence-gap recording for a notification dropped at the adapter.

    Same loop resolution as :func:`schedule_node_datachange`: the adapter callback may run
    on another thread, so the gap is queued on the loop that serves the subscriptions.
    """
    target_loop = service._event_loop
    if target_loop is not None and target_loop.is_closed():
        target_loop = None

    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if target_loop is None and current_loop is not None:
        service._event_loop = current_loop
        service._ensure_cleanup_task()
        service._enqueue_notification_gap(bin_id)
        return

    if current_loop is not None and target_loop is current_loop:
        service._ensure_cleanup_task()
        service._enqueue_notification_gap(bin_id)
        return

    if target_loop is None:
        logger.warning(
            "Dropping notification gap recording because no running event loop is available",
            extra={"bin_id": bin_id},
        )
        return

    target_loop.call_soon_threadsafe(service._ensure_cleanup_task)
    target_loop.call_soon_threadsafe(service._enqueue_notification_gap, bin_id)


async def record_notification_gap(service: SubscriptionService, bin_id: str) -> None:
    """Advance the sequence of every subscription attached to ``bin_id`` by the lost count.

    The dropped notification's node id is unknown, so the loss is attributed to the bin that
    published it. A bin that is gone (reclaimed or never committed) belongs to no
    subscription, so nothing is recorded. The markers match buffer-overflow reporting, so
    sync answers with the dropped range and streams see the sequence jump.
    """
    count = 1 + service._dropped_notification_gaps.pop(bin_id, 0)
    async with service._lock:
        bin_ = service._bins.get(bin_id)
        if bin_ is None:
            logger.debug("Notification gap for missing bin bin_id=%s; nothing to record", bin_id)
            return
        # One subscription may monitor several nodes of the bin: record the gap once per
        # subscription (one lost notification is one sequence step).
        states: dict[str, state._SubscriptionState] = {}
        for node_id in bin_.node_handles:
            monitor = service._node_monitors.get(node_id)
            if monitor is None or monitor.bin_id != bin_id:
                continue
            for subscription_id in monitor.subscriber_ids:
                subscription_state = service._subscriptions.get(subscription_id)
                if subscription_state is not None:
                    states[subscription_id] = subscription_state

    for subscription_state in states.values():
        async with subscription_state.lock:
            if subscription_state.dropped_from_sequence is None:
                subscription_state.dropped_from_sequence = subscription_state.sequence_number + 1
            subscription_state.sequence_number += count
            subscription_state.dropped_to_sequence = subscription_state.sequence_number
            subscription_state.update_event.set()

    logger.warning(
        "Notification gap recorded bin_id=%s count=%d subscriptions=%d",
        bin_id,
        count,
        len(states),
    )


def updates_after(subscription_state: state._SubscriptionState, after_sequence: int) -> list[SubscriptionUpdate]:
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


def append_update(
    service: SubscriptionService,
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
    quality = quality_for_payload(value, signal.quality)

    has_current = node_id in subscription_state.last_values_by_node_id
    current = subscription_state.last_values_by_node_id.get(node_id)
    current_quality = subscription_state.last_quality_by_node_id.get(node_id)
    if has_current and current == value and current_quality == quality:
        return

    subscription_state.last_values_by_node_id[node_id] = value
    subscription_state.last_quality_by_node_id[node_id] = quality
    subscription_state.sequence_number += 1
    if element_id is None:
        element_id = service._resolve_element_id(subscription_state, node_id)
    if len(subscription_state.updates) >= service._max_updates_per_subscription:
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
            timestamp=signal.timestamp or now_iso(),
        )
    )
    subscription_state.update_event.set()
