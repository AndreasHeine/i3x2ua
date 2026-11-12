"""OPC UA monitoring runtime for the subscription service.

Native bin packing (shared OPC UA subscriptions), per-node monitor start/stop,
the shared polling loop, and the reconnect resync. These functions receive the
owning ``SubscriptionService`` and operate on its state under its lock; the
subscription-facing logic stays in ``service.py``.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from i3x_server.domain.ports.opcua import OpcUaSubscriptionCapabilities
from i3x_server.infrastructure.subscriptions.state import (
    _ManagedBin,
    _NodeMonitor,
    _signal_from_data_value,
    _ValueSignal,
)
from i3x_server.schemas.opcua_value import decode_data_value, notification_data_value

if TYPE_CHECKING:
    from i3x_server.infrastructure.subscriptions.service import SubscriptionService

logger = logging.getLogger(__name__)

# OPC UA service result codes that report a *subscription/session* limit, not a rejection of this
# single monitored item (OPC UA Part 4, service result codes). Only these justify backing off native
# admissions service-wide; an unsupported node must degrade that node alone to polling.
_SUBSCRIPTION_LEVEL_STATUS_CODES = frozenset(
    {
        0x80770000,  # BadTooManySubscriptions
        0x80560000,  # BadTooManySessions
        0x80DB0000,  # BadTooManyMonitoredItems
        0x80280000,  # BadSubscriptionIdInvalid
    }
)


def _status_code_of(exc: BaseException) -> int | None:
    """The OPC UA status code of an upstream failure.

    ``asyncua.ua.uaerrors.UaStatusCodeError`` exposes ``code`` as an ``int``; the runtime deliberately
    stays free of an asyncua import, so the code is read structurally.
    """
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


def _is_subscription_level_failure(exc: BaseException) -> bool:
    return _status_code_of(exc) in _SUBSCRIPTION_LEVEL_STATUS_CODES


class _NodeDataChangeHandler:
    """Datachange handler for a shared, potentially multi-node OPC UA subscription (bin)."""

    def __init__(self, service: SubscriptionService, bin_id: str) -> None:
        self._service = service
        self._bin_id = bin_id

    def datachange_notification(self, node: Any, val: Any, data: Any) -> None:
        signal = _signal_from_data_value(notification_data_value(data))
        try:
            node_id = node.nodeid.to_string()
        except Exception:
            logger.warning(
                "Unable to resolve node_id for datachange in bin_id=%s; dropping update",
                self._bin_id,
            )
            return
        self._service.schedule_node_datachange(node_id, val, signal)

    def event_notification(self, event: Any) -> None:
        return None


async def _handle_client_reconnect(service: SubscriptionService) -> None:
    """Re-sync native bins after asyncua's supervisor restored its subscriptions.

    asyncua transfers or recreates every OPC UA subscription it tracks, keeping our
    notification handlers. Only items it could not restore are re-admitted here; a bin
    whose subscription is gone is dropped and its nodes are re-admitted.
    """
    if service._closed:
        # The service is shutting down; re-admitting against a disconnecting client would leak tasks.
        return
    dead_bins: list[_ManagedBin] = []
    lost_node_ids: list[str] = []
    async with service._lock:
        for bin_id, bin_ in list(service._bins.items()):
            if bin_.ua_subscription is None:
                # Placeholder whose create round-trip is still in flight; its owner re-syncs it.
                continue
            handles = service._opcua_client.get_monitored_item_handles(bin_.ua_subscription, list(bin_.node_handles))
            missing = [node_id for node_id in bin_.node_handles if handles is None or node_id not in handles]
            if handles:
                bin_.node_handles = handles
            else:
                del service._bins[bin_id]
                dead_bins.append(bin_)
            for node_id in missing:
                monitor = service._node_monitors.get(node_id)
                if monitor is not None and monitor.bin_id == bin_id:
                    monitor.bin_id = None
                    monitor.mode = "idle"
                    lost_node_ids.append(node_id)
            if missing:
                logger.warning(
                    "Native bin lost monitored items after reconnect bin_id=%s lost=%d kept=%d",
                    bin_id,
                    len(missing),
                    len(handles or {}),
                )

    for bin_ in dead_bins:
        with suppress(Exception):
            await service._opcua_client.delete_subscription(bin_.ua_subscription)

    # Re-admit lost monitors concurrently: selection stays serialized for bin packing and the
    # round-trips run under the native admission semaphore, like normal registration.
    if lost_node_ids:
        resync_monitors: list[_NodeMonitor] = []
        for node_id in sorted(lost_node_ids):
            async with service._lock:
                monitor = service._node_monitors.get(node_id)
            if monitor is not None and monitor.mode == "idle":
                resync_monitors.append(monitor)
        if resync_monitors:
            await asyncio.gather(*(_start_node_monitor_runtime(service, monitor) for monitor in resync_monitors))

    async with service._lock:
        subscription_ids = list(service._subscriptions.keys())
    for subscription_id in subscription_ids:
        await service._update_subscription_mode(subscription_id)


def _capped_limit(server_limit: int | None, local_cap: int) -> int:
    """Effective cap: the tighter of the server-advertised limit and the process-local floor.

    Servers that advertise no capability for a dimension report ``None``; without a floor the
    admission checks below would be skipped entirely and bins/monitored items could grow without
    bound, so the process-local cap applies instead.
    """
    if server_limit is None or server_limit <= 0:
        return local_cap
    return min(server_limit, local_cap)


def _can_open_new_bin(service: SubscriptionService, caps: OpcUaSubscriptionCapabilities) -> bool:
    """Whether a further native bin may be opened. Must be called while holding ``service._lock``.

    Reserved slots from in-flight admissions count towards the monitored-item budget, so the cap
    stays exact while network round-trips run concurrently.
    """
    bin_count = len(service._bins)
    total_monitored = sum(len(bin_.node_handles) + bin_.pending_adds for bin_ in service._bins.values())

    if bin_count + 1 > _capped_limit(caps.max_subscriptions, service._native_max_bins):
        return False
    if bin_count + 1 > _capped_limit(caps.max_subscriptions_per_session, service._native_max_bins):
        return False
    if total_monitored + 1 > _capped_limit(caps.max_monitored_items, service._native_max_monitored_items):
        return False
    return True


def _extract_revised_subscription_parameters(ua_subscription: Any) -> tuple[float | None, int | None]:
    publishing_interval_ms = _positive_float_or_none(
        _read_attr_chain(ua_subscription, "RevisedPublishingInterval")
        or _read_attr_chain(ua_subscription, "revised_publishing_interval")
        or _read_attr_chain(ua_subscription, "data", "RevisedPublishingInterval")
        or _read_attr_chain(ua_subscription, "result", "RevisedPublishingInterval")
        or _read_attr_chain(ua_subscription, "parameters", "RevisedPublishingInterval")
    )
    max_keepalive_count = _positive_int_or_none(
        _read_attr_chain(ua_subscription, "RevisedMaxKeepAliveCount")
        or _read_attr_chain(ua_subscription, "revised_max_keepalive_count")
        or _read_attr_chain(ua_subscription, "data", "RevisedMaxKeepAliveCount")
        or _read_attr_chain(ua_subscription, "result", "RevisedMaxKeepAliveCount")
        or _read_attr_chain(ua_subscription, "parameters", "RevisedMaxKeepAliveCount")
    )
    return publishing_interval_ms, max_keepalive_count


def _compute_native_refresh_interval_seconds(service: SubscriptionService, ua_subscription: Any) -> float:
    publishing_interval_ms, max_keepalive_count = _extract_revised_subscription_parameters(ua_subscription)
    if publishing_interval_ms is None or max_keepalive_count is None:
        fallback = min(
            service._native_timeout_refresh_max_seconds,
            service._interval_seconds * service._native_timeout_refresh_keepalives,
        )
        return max(0.1, fallback)

    keepalive_period_seconds = (publishing_interval_ms / 1000.0) * max_keepalive_count
    target_interval_seconds = keepalive_period_seconds * service._native_timeout_refresh_keepalives
    return max(0.1, min(service._native_timeout_refresh_max_seconds, target_interval_seconds))


async def _start_node_monitor_runtime(service: SubscriptionService, monitor: _NodeMonitor) -> None:
    if service._closed:
        # Shutdown in progress: do not open OPC UA subscriptions against a disconnecting client.
        return

    if service._now_monotonic() < service._native_backoff_until_monotonic:
        await _start_polling_node_monitor(service, monitor)
        return

    caps = await service._opcua_client.get_subscription_capabilities()
    try:
        acquired = await _acquire_native_bin_slot(service, monitor, caps)
    except Exception as exc:
        if _is_subscription_level_failure(exc):
            # The server reported a subscription/session limit: retrying every node right away
            # would fail identically, so native admissions pause service-wide.
            logger.exception(
                "Native OPC UA monitor failed for node_id=%s (subscription-level status code); "
                "backing off native admissions for %.1fs",
                monitor.node_id,
                service._native_backoff_seconds,
            )
            service._native_backoff_until_monotonic = service._now_monotonic() + service._native_backoff_seconds
        else:
            # A per-item rejection (unsupported node, invalid id, ...) only degrades this node.
            # Other nodes must keep monitoring natively instead of a service-wide backoff.
            logger.warning(
                "Native OPC UA monitor rejected for node_id=%s (status code=%s); this node falls back to polling",
                monitor.node_id,
                _status_code_of(exc),
            )
        acquired = False

    if not acquired:
        await _start_polling_node_monitor(service, monitor)


async def _acquire_native_bin_slot(
    service: SubscriptionService, monitor: _NodeMonitor, caps: OpcUaSubscriptionCapabilities
) -> bool:
    """Reserve a bin under the admission lock, then run the OPC UA round-trip outside it.

    Selection stays serialized so concurrent admissions *pack* into one bin instead of opening
    several: when no bin has spare capacity a placeholder bin is registered here and reserved with
    ``pending_adds``; the round-trips themselves run under ``_native_admission_semaphore``. That
    makes ``max_concurrent_native_admissions`` effective - it used to be nested inside the selection
    lock, so at most one admission could ever be in flight.
    """
    async with service._native_bin_admission_lock:
        capacity = _min_positive(caps.max_monitored_items_per_subscription)
        # A server advertising no per-subscription capacity gets the default fallback, bounded by
        # the process-local monitored-item budget; an advertised capacity stays verbatim (the
        # total-budget check below is what limits how many items are actually admitted).
        effective_capacity = (
            capacity if capacity is not None else min(_DEFAULT_BIN_CAPACITY, service._native_max_monitored_items)
        )

        async with service._lock:
            total_monitored = sum(len(bin_.node_handles) + bin_.pending_adds for bin_ in service._bins.values())
            if total_monitored + 1 > _capped_limit(caps.max_monitored_items, service._native_max_monitored_items):
                return False
            target_bin = next(
                (bin_ for bin_ in service._bins.values() if len(bin_.node_handles) + bin_.pending_adds < bin_.capacity),
                None,
            )
            opening = target_bin is None
            if target_bin is None:
                if not _can_open_new_bin(service, caps):
                    return False
                target_bin = _ManagedBin(
                    bin_id=f"bin-{uuid4()}",
                    ua_subscription=None,
                    capacity=effective_capacity,
                )
                service._bins[target_bin.bin_id] = target_bin
            target_bin.pending_adds += 1

    if opening:
        return await _open_new_bin(service, monitor, target_bin)
    return await _add_node_to_bin(service, monitor, target_bin)


async def _subscribe_single_node(service: SubscriptionService, ua_subscription: Any, node_id: str) -> int | None:
    handles = await service._opcua_client.subscribe_data_changes(ua_subscription, [node_id])
    if isinstance(handles, list):
        handle = handles[0] if handles else None
    else:
        handle = handles
    return _to_client_handle(handle)


async def _open_new_bin(service: SubscriptionService, monitor: _NodeMonitor, placeholder: _ManagedBin) -> bool:
    """Fill the placeholder bin that ``_acquire_native_bin_slot`` reserved with an OPC UA subscription."""
    ua_subscription: Any = None
    handle: int | None = None
    failed = False
    try:
        async with service._native_admission_semaphore:
            handler = _NodeDataChangeHandler(service, placeholder.bin_id)
            ua_subscription = await service._opcua_client.create_datachange_subscription(
                publishing_interval_ms=float(service._interval_seconds * 1000),
                handler=handler,
            )
            placeholder.native_refresh_interval_seconds = _compute_native_refresh_interval_seconds(
                service, ua_subscription
            )
            placeholder.ua_subscription = ua_subscription
            placeholder.ready.set()
            handle = await _subscribe_single_node(service, ua_subscription, monitor.node_id)
    except Exception:
        failed = True
        raise
    finally:
        # Admissions that reserved a slot in this bin must not wait for a create that never succeeds.
        placeholder.ready.set()
        if failed and ua_subscription is not None:
            # The create succeeded but the item add raised: asyncua has no registry of the
            # subscription, so it would keep publishing (and hold a session slot) forever.
            with suppress(Exception):
                await service._opcua_client.delete_subscription(ua_subscription)
        async with service._lock:
            reserved_bin = service._bins.get(placeholder.bin_id)
            if reserved_bin is not None:
                reserved_bin.pending_adds = max(0, reserved_bin.pending_adds - 1)
                if failed and not reserved_bin.node_handles:
                    del service._bins[placeholder.bin_id]

    if handle is None:
        with suppress(Exception):
            await service._opcua_client.delete_subscription(ua_subscription)
        async with service._lock:
            reserved_bin = service._bins.get(placeholder.bin_id)
            if reserved_bin is not None and not reserved_bin.node_handles:
                del service._bins[placeholder.bin_id]
        return False

    stale_subscription: Any = None
    async with service._lock:
        live = service._node_monitors.get(monitor.node_id)
        if live is None or live is not monitor or service._bins.get(placeholder.bin_id) is None:
            # Monitor released concurrently (no subscribers left), or the placeholder was reclaimed.
            stale_subscription = ua_subscription
        else:
            placeholder.node_handles[monitor.node_id] = handle
            live.bin_id = placeholder.bin_id
            live.mode = "native"
            live.native_refresh_interval_seconds = placeholder.native_refresh_interval_seconds

    if stale_subscription is not None:
        # Delete outside the lock so a slow round-trip never blocks fan-out, sync, or SSE delivery.
        await service._opcua_client.delete_subscription(stale_subscription)
        return False

    logger.info(
        "Node monitor bin opened bin_id=%s node_id=%s capacity=%d",
        placeholder.bin_id,
        monitor.node_id,
        placeholder.capacity,
    )
    return True


async def _add_node_to_bin(service: SubscriptionService, monitor: _NodeMonitor, target_bin: _ManagedBin) -> bool:
    handle: int | None = None
    try:
        if not target_bin.ready.is_set():
            # The bin is still being created: wait outside the semaphore and outside any lock.
            await target_bin.ready.wait()
        async with service._native_admission_semaphore:
            ua_subscription = target_bin.ua_subscription
            if ua_subscription is None:
                # The creating admission failed; this node falls back to polling.
                return False
            handle = await _subscribe_single_node(service, ua_subscription, monitor.node_id)
    finally:
        async with service._lock:
            reserved_bin = service._bins.get(target_bin.bin_id)
            if reserved_bin is not None:
                reserved_bin.pending_adds = max(0, reserved_bin.pending_adds - 1)
    if handle is None:
        return False

    stale: tuple[Any, int] | None = None
    async with service._lock:
        live = service._node_monitors.get(monitor.node_id)
        live_bin = service._bins.get(target_bin.bin_id)
        if live is None or live is not monitor or live_bin is None:
            # Monitor released concurrently, or the bin was reclaimed/closed under us.
            stale = (target_bin.ua_subscription, handle)
        else:
            live_bin.node_handles[monitor.node_id] = handle
            live.bin_id = live_bin.bin_id
            live.mode = "native"
            live.native_refresh_interval_seconds = live_bin.native_refresh_interval_seconds

    if stale is not None:
        # Cleanup must not run while the lock is held: it would block every other path
        # (fan-out, sync, SSE delivery) for a full upstream round-trip.
        with suppress(Exception):
            await service._opcua_client.unsubscribe_data_changes(stale[0], [stale[1]])
        return False

    logger.info(
        "Node monitor added to bin bin_id=%s node_id=%s size=%d/%d",
        target_bin.bin_id,
        monitor.node_id,
        len(target_bin.node_handles),
        target_bin.capacity,
    )
    return True


async def _start_polling_node_monitor(service: SubscriptionService, monitor: _NodeMonitor) -> None:
    async with service._lock:
        live = service._node_monitors.get(monitor.node_id)
        if live is None or live is not monitor:
            return
        live.mode = "polling"
        live.bin_id = None
        live.native_refresh_interval_seconds = None
    logger.info(
        "Node monitor runtime started node_id=%s mode=polling interval_s=%.3f",
        monitor.node_id,
        service._interval_seconds,
    )


async def _stop_node_monitor_runtime(service: SubscriptionService, monitor: _NodeMonitor) -> None:
    bin_id = monitor.bin_id
    monitor.bin_id = None
    monitor.mode = "idle"
    monitor.native_refresh_interval_seconds = None

    if bin_id is None:
        return

    async with service._lock:
        target_bin = service._bins.get(bin_id)
        handle = target_bin.node_handles.pop(monitor.node_id, None) if target_bin is not None else None
        bin_now_empty = target_bin is not None and not target_bin.node_handles
        if bin_now_empty:
            del service._bins[bin_id]

    if target_bin is None:
        return

    if bin_now_empty:
        try:
            await service._opcua_client.delete_subscription(target_bin.ua_subscription)
        except Exception:
            logger.debug(
                "Ignoring delete failure for empty bin bin_id=%s",
                bin_id,
                exc_info=True,
            )
        logger.info("Node monitor bin reclaimed bin_id=%s", bin_id)
    elif handle is not None:
        try:
            await service._opcua_client.unsubscribe_data_changes(target_bin.ua_subscription, [handle])
        except Exception:
            logger.debug(
                "Ignoring unsubscribe failure bin_id=%s node_id=%s",
                bin_id,
                monitor.node_id,
                exc_info=True,
            )


async def _shared_polling_loop(service: SubscriptionService) -> None:
    """Single batched read loop covering every node currently in polling mode.

    Ticks are scheduled against an absolute deadline: the read itself contributes
    no drift, so a slow tick shortens the following sleep instead of pushing the
    whole schedule backwards. Fan-out for one tick runs concurrently rather than
    sequentially, so N nodes do not serialize N lock round-trips per tick.
    """
    loop = asyncio.get_running_loop()
    interval = service._interval_seconds
    next_deadline = loop.time() + interval
    try:
        while True:
            # Only acquire the lock when there are monitored nodes; an idle service
            # must not serialise admissions behind a tick that reads nothing.
            node_ids: list[str] = []
            if service._node_monitors:
                async with service._lock:
                    node_ids = sorted(
                        node_id for node_id, monitor in service._node_monitors.items() if monitor.mode == "polling"
                    )

            if node_ids:
                try:
                    data_values = await service._opcua_client.read_data_values(node_ids)
                    if len(data_values) != len(node_ids):
                        raise RuntimeError(
                            f"Read returned {len(data_values)} values for {len(node_ids)} monitored nodes"
                        )
                except Exception:
                    logger.exception("Shared polling read failed node_count=%d", len(node_ids))
                else:
                    fanouts = []
                    for node_id, data_value in zip(node_ids, data_values, strict=True):
                        payload, quality, timestamp = decode_data_value(data_value)
                        fanouts.append(service._fanout_node_value(node_id, payload, _ValueSignal(quality, timestamp)))
                    await asyncio.gather(*fanouts)

            delay = next_deadline - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
                next_deadline += interval
            else:
                # The tick overshot its deadline; realign instead of catching up in a burst.
                next_deadline = loop.time() + interval
    except asyncio.CancelledError:
        return


def _min_positive(*values: int | None) -> int | None:
    positive = [value for value in values if value is not None and value > 0]
    if not positive:
        return None
    return min(positive)


_DEFAULT_BIN_CAPACITY = 1000
"""Fallback per-subscription monitored-item capacity when the server advertises none."""


def _positive_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def _positive_float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def _read_attr_chain(root: Any, *names: str) -> Any:
    current = root
    for name in names:
        if current is None:
            return None
        current = getattr(current, name, None)
    return current


def _to_client_handle(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        pass

    nested_value = getattr(value, "Value", None)
    if nested_value is None:
        return None
    try:
        return int(nested_value)
    except (TypeError, ValueError):
        return None
