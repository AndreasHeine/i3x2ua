"""Shared node monitors: acquisition, release, subscription mode, value refresh.

A node monitor is shared across subscriptions; the native bin and polling runtime lives
in ``monitoring.py``. Value refresh reads through the OPC UA port outside the registry
lock and appends under each subscription's own lock.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from i3x_server.infrastructure.subscriptions import monitoring, state
from i3x_server.schemas.opcua_value import decode_data_value, normalize_opcua_value

if TYPE_CHECKING:
    from i3x_server.infrastructure.subscriptions.service import SubscriptionService

logger = logging.getLogger(__name__)


async def _update_subscription_mode(service: SubscriptionService, subscription_id: str) -> None:
    async with service._lock:
        subscription_state = service._subscriptions.get(subscription_id)
        if subscription_state is None:
            return
        if not subscription_state.monitored_node_ids:
            subscription_state.mode = "idle"
            subscription_state.native_refresh_interval_seconds = None
            return

        modes: list[str] = []
        native_intervals: list[float] = []
        for node_id in subscription_state.monitored_node_ids:
            monitor = service._node_monitors.get(node_id)
            mode = monitor.mode if monitor is not None else "idle"
            modes.append(mode)
            if monitor is not None and monitor.mode == "native" and monitor.native_refresh_interval_seconds is not None:
                native_intervals.append(monitor.native_refresh_interval_seconds)

        if modes and all(mode == "native" for mode in modes):
            subscription_state.mode = "native"
            subscription_state.native_refresh_interval_seconds = min(native_intervals) if native_intervals else None
        else:
            subscription_state.mode = "polling"
            subscription_state.native_refresh_interval_seconds = None


async def _acquire_node_monitor(service: SubscriptionService, node_id: str, subscription_id: str) -> None:
    async with service._lock:
        monitor = service._node_monitors.get(node_id)
        if monitor is not None:
            monitor.subscriber_ids.add(subscription_id)
            return
        monitor = state._NodeMonitor(node_id=node_id)
        monitor.subscriber_ids.add(subscription_id)
        service._node_monitors[node_id] = monitor

    await monitoring._start_node_monitor_runtime(service, monitor)


async def _release_node_monitor(service: SubscriptionService, node_id: str, subscription_id: str) -> None:
    async with service._lock:
        monitor = service._node_monitors.get(node_id)
        if monitor is None:
            return
        monitor.subscriber_ids.discard(subscription_id)
        should_stop = not monitor.subscriber_ids
        if should_stop:
            del service._node_monitors[node_id]
            # Nothing will deliver this node anymore: queued drop counts would fabricate a
            # sequence gap no consumer can observe and leak the entry forever.
            service._dropped_node_datachanges.pop(node_id, None)

    if should_stop:
        await monitoring._stop_node_monitor_runtime(service, monitor)


async def _seed_initial_updates(service: SubscriptionService, subscription_id: str) -> None:
    if not service._seed_initial_values:
        return

    await service._refresh_changed_values(subscription_id)


async def _refresh_changed_values(service: SubscriptionService, subscription_id: str) -> None:
    node_ids: list[str] = []

    async with service._lock:
        subscription_state = service._subscriptions.get(subscription_id)
        if subscription_state is None or subscription_state.mode == "idle":
            return
        node_ids = sorted(subscription_state.monitored_node_ids)

    if not node_ids:
        return

    try:
        data_values = await service._opcua_client.read_data_values(node_ids)
        if len(data_values) != len(node_ids):
            raise RuntimeError(f"Read returned {len(data_values)} values for {len(node_ids)} monitored nodes")
    except Exception:
        logger.exception("Initial subscription snapshot failed", extra={"subscription_id": subscription_id})
        return

    decoded: list[tuple[str, Any, state._ValueSignal]] = []
    for node_id, data_value in zip(node_ids, data_values, strict=True):
        payload, quality, timestamp = decode_data_value(data_value)
        decoded.append((node_id, normalize_opcua_value(payload), state._ValueSignal(quality, timestamp)))

    async with service._lock:
        subscription_state = service._subscriptions.get(subscription_id)
        if subscription_state is None:
            return
        delivered = [
            (node_id, payload, signal, service._resolve_element_id(subscription_state, node_id))
            for node_id, payload, signal in decoded
            if node_id in subscription_state.monitored_node_ids
        ]
        service._touch(subscription_state)

    async with subscription_state.lock:
        for node_id, payload, signal, element_id in delivered:
            service._append_update(subscription_state, node_id, payload, signal, element_id=element_id)


async def _handle_datachange(
    service: SubscriptionService,
    subscription_id: str,
    node_id: str,
    value: Any,
    signal: state._ValueSignal = state._NO_SIGNAL,
) -> None:
    # Normalise before taking the lock: the value coercion (variant decode,
    # container unpack) is the expensive part and must not serialise other
    # subscriptions behind it.
    normalized_value = normalize_opcua_value(value)
    async with service._lock:
        subscription_state = service._subscriptions.get(subscription_id)
        if subscription_state is None:
            return

        resolved_node_id = service._resolve_target_node(subscription_state, node_id)
        if resolved_node_id is None:
            return
        # Mapping lookup stays under the registry lock; the buffer append then runs under
        # this subscription's own lock, so deliveries for other subscriptions are not
        # blocked behind it.
        element_id = service._resolve_element_id(subscription_state, resolved_node_id)
        logger.debug(
            "Subscription datachange callback subscription_id=%s mode=%s node_id=%s",
            subscription_id,
            subscription_state.mode,
            resolved_node_id,
        )
        # Removed touch() here to ensure server notifications don't keep stale subscriptions alive.
        # Only client activity (sync, wait_for_updates) should refresh the TTL.

    async with subscription_state.lock:
        service._append_update(subscription_state, resolved_node_id, normalized_value, signal, element_id=element_id)
        subscription_state.native_timeout_count = 0
