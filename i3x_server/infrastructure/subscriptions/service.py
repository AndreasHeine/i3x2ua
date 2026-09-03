from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import ceil
from typing import Any
from uuid import uuid4

from i3x_server.config.settings import get_settings
from i3x_server.domain.ports.opcua import OpcUaClientProtocol, OpcUaSubscriptionCapabilities
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult

logger = logging.getLogger(__name__)


def _stream_debug_enabled() -> bool:
    return get_settings().debug_subscription_stream


@dataclass(slots=True)
class SubscriptionUpdate:
    sequence_number: int
    element_id: str
    node_id: str
    value: Any
    quality: str
    timestamp: str


@dataclass(slots=True)
class SubscriptionSyncResult:
    updates: list[SubscriptionUpdate]
    queue_overflow: bool = False
    dropped_from_sequence: int | None = None
    dropped_to_sequence: int | None = None
    stream_active: bool = False


@dataclass(slots=True)
class SubscriptionDeleteResult:
    success: bool
    subscription_id: str
    error: dict[str, Any] | None = None


@dataclass(slots=True)
class SubscriptionDetail:
    subscription_id: str
    client_id: str | None
    display_name: str | None
    monitored_objects: list[dict[str, Any]]
    mode: str


@dataclass(slots=True)
class _SubscriptionState:
    subscription_id: str
    client_id: str | None
    display_name: str | None
    monitored_objects: dict[str, int]
    monitored_node_ids: set[str] = field(default_factory=set)
    node_to_element_id: dict[str, str] = field(default_factory=dict)
    handle_to_node_id: dict[int, str] = field(default_factory=dict)
    last_values_by_node_id: dict[str, Any] = field(default_factory=dict)
    updates: list[SubscriptionUpdate] = field(default_factory=list)
    sequence_number: int = 0
    mode: str = "idle"
    update_event: asyncio.Event = field(default_factory=asyncio.Event)
    active_stream_generation: int = 0
    stream_connected: bool = False
    dropped_from_sequence: int | None = None
    dropped_to_sequence: int | None = None
    last_activity_monotonic: float = 0.0
    native_refresh_interval_seconds: float | None = None
    native_timeout_count: int = 0


@dataclass(slots=True)
class _NodeMonitor:
    """Shared OPC UA monitoring runtime for one node_id, fanned out to N subscriptions.

    Reference-counted across every i3X subscription that monitors this node_id,
    instead of one dedicated runtime per subscription. In native mode the node is
    packed into a shared `_ManagedBin` (one OPC UA subscription can hold many
    monitored items); in polling mode it is read by the single shared polling loop.
    """

    node_id: str
    mode: str = "idle"
    subscriber_ids: set[str] = field(default_factory=set)
    bin_id: str | None = None
    native_refresh_interval_seconds: float | None = None


@dataclass(slots=True)
class _ManagedBin:
    """One native OPC UA subscription shared by multiple monitored nodes (Phase 2 bin-packing)."""

    bin_id: str
    ua_subscription: Any
    capacity: int
    node_handles: dict[str, int] = field(default_factory=dict)
    native_refresh_interval_seconds: float | None = None


class _DataChangeHandler:
    def __init__(self, service: SubscriptionService, subscription_id: str) -> None:
        self._service = service
        self._subscription_id = subscription_id

    def datachange_notification(self, node: Any, val: Any, data: Any) -> None:
        try:
            node_id = node.nodeid.to_string()
        except Exception:
            node_id = str(node)
        client_handle: int | None = None
        monitored_item = getattr(data, "monitored_item", None)
        if monitored_item is not None:
            raw_handle = getattr(monitored_item, "ClientHandle", None)
            client_handle = _to_client_handle(raw_handle)

        self._service.schedule_datachange(self._subscription_id, node_id, val, client_handle)

    def event_notification(self, event: Any) -> None:
        return None


class _NodeDataChangeHandler:
    """Datachange handler for a shared, potentially multi-node OPC UA subscription (bin)."""

    def __init__(self, service: SubscriptionService, bin_id: str) -> None:
        self._service = service
        self._bin_id = bin_id

    def datachange_notification(self, node: Any, val: Any, data: Any) -> None:
        del data
        try:
            node_id = node.nodeid.to_string()
        except Exception:
            logger.warning(
                "Unable to resolve node_id for datachange in bin_id=%s; dropping update",
                self._bin_id,
            )
            return
        self._service.schedule_node_datachange(node_id, val)

    def event_notification(self, event: Any) -> None:
        return None


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
        self._subscriptions: dict[str, _SubscriptionState] = {}
        self._node_monitors: dict[str, _NodeMonitor] = {}
        self._bins: dict[str, _ManagedBin] = {}
        self._native_admission_semaphore = asyncio.Semaphore(max(1, int(max_concurrent_native_admissions)))
        self._native_backoff_seconds = max(0.1, float(native_backoff_seconds))
        self._native_backoff_until_monotonic: float = 0.0
        self._cleanup_task: asyncio.Task[None] | None = None
        self._datachange_task: asyncio.Task[None] | None = None
        self._node_datachange_task: asyncio.Task[None] | None = None
        self._shared_polling_task: asyncio.Task[None] | None = None
        self._datachange_queue: asyncio.Queue[tuple[str, str, Any, int | None]] = asyncio.Queue(
            maxsize=max(1000, self._max_updates_per_subscription)
        )
        self._node_datachange_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(
            maxsize=max(1000, self._max_updates_per_subscription)
        )
        self._dropped_datachange_events = 0
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._shutdown_event: asyncio.Event = asyncio.Event()
        self._reconnect_listener = self._handle_client_reconnect
        self._opcua_client.add_reconnect_listener(self._reconnect_listener)

    def _remember_event_loop(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._event_loop is None or self._event_loop.is_closed():
            self._event_loop = loop

    def _enqueue_datachange(
        self,
        subscription_id: str,
        node_id: str,
        value: Any,
        client_handle: int | None,
    ) -> None:
        if self._datachange_queue.full():
            try:
                self._datachange_queue.get_nowait()
                self._dropped_datachange_events += 1
            except asyncio.QueueEmpty:
                pass
        try:
            self._datachange_queue.put_nowait((subscription_id, node_id, value, client_handle))
        except asyncio.QueueFull:
            self._dropped_datachange_events += 1

    async def _datachange_loop(self) -> None:
        try:
            while True:
                subscription_id, node_id, value, client_handle = await self._datachange_queue.get()
                try:
                    await self.handle_datachange(subscription_id, node_id, value, client_handle)
                except Exception:
                    logger.exception(
                        "OPC UA datachange handling failed",
                        extra={
                            "subscription_id": subscription_id,
                            "node_id": node_id,
                            "client_handle": client_handle,
                        },
                    )
        except asyncio.CancelledError:
            return

    def _enqueue_node_datachange(self, node_id: str, value: Any) -> None:
        if self._node_datachange_queue.full():
            try:
                self._node_datachange_queue.get_nowait()
                self._dropped_datachange_events += 1
            except asyncio.QueueEmpty:
                pass
        try:
            self._node_datachange_queue.put_nowait((node_id, value))
        except asyncio.QueueFull:
            self._dropped_datachange_events += 1

    async def _node_datachange_loop(self) -> None:
        try:
            while True:
                node_id, value = await self._node_datachange_queue.get()
                try:
                    await self._fanout_node_value(node_id, value)
                except Exception:
                    logger.exception(
                        "Shared node datachange fan-out failed",
                        extra={"node_id": node_id},
                    )
        except asyncio.CancelledError:
            return

    async def _fanout_node_value(self, node_id: str, value: Any) -> None:
        """Deliver one node-level value change to every subscription monitoring it."""
        async with self._lock:
            monitor = self._node_monitors.get(node_id)
            subscriber_ids = list(monitor.subscriber_ids) if monitor is not None else []
        for subscription_id in subscriber_ids:
            await self.handle_datachange(subscription_id, node_id, value)

    def schedule_node_datachange(self, node_id: str, value: Any) -> None:
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
            self._enqueue_node_datachange(node_id, value)
            return

        if current_loop is not None and target_loop is current_loop:
            self._ensure_cleanup_task()
            self._enqueue_node_datachange(node_id, value)
            return

        if target_loop is None:
            logger.warning(
                "Dropping shared node datachange because no running event loop is available",
                extra={"node_id": node_id},
            )
            return

        target_loop.call_soon_threadsafe(self._ensure_cleanup_task)
        target_loop.call_soon_threadsafe(self._enqueue_node_datachange, node_id, value)

    def schedule_datachange(
        self,
        subscription_id: str,
        node_id: str,
        value: Any,
        client_handle: int | None = None,
    ) -> None:
        target_loop = self._event_loop
        if target_loop is not None and target_loop.is_closed():
            target_loop = None

        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if target_loop is None and current_loop is not None:
            # Capture the app loop lazily on first callback when not already known.
            self._event_loop = current_loop
            self._ensure_cleanup_task()
            self._enqueue_datachange(subscription_id, node_id, value, client_handle)
            return

        if current_loop is not None and target_loop is current_loop:
            self._ensure_cleanup_task()
            self._enqueue_datachange(subscription_id, node_id, value, client_handle)
            return

        if target_loop is None:
            logger.warning(
                "Dropping OPC UA datachange because no running event loop is available",
                extra={"subscription_id": subscription_id, "node_id": node_id},
            )
            return

        target_loop.call_soon_threadsafe(
            self._ensure_cleanup_task,
        )
        target_loop.call_soon_threadsafe(
            self._enqueue_datachange,
            subscription_id,
            node_id,
            value,
            client_handle,
        )

    def _now_monotonic(self) -> float:
        self._remember_event_loop()
        return asyncio.get_running_loop().time()

    def _touch(self, state: _SubscriptionState) -> None:
        state.last_activity_monotonic = self._now_monotonic()

    def _ensure_cleanup_task(self) -> None:
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        if self._datachange_task is None or self._datachange_task.done():
            self._datachange_task = asyncio.create_task(self._datachange_loop())
        if self._node_datachange_task is None or self._node_datachange_task.done():
            self._node_datachange_task = asyncio.create_task(self._node_datachange_loop())
        if self._shared_polling_task is None or self._shared_polling_task.done():
            self._shared_polling_task = asyncio.create_task(self._shared_polling_loop())

    async def _cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(max(0.1, self._interval_seconds))
                stale_ids: list[str] = []
                now = self._now_monotonic()
                async with self._lock:
                    for state in self._subscriptions.values():
                        if now - state.last_activity_monotonic >= self._ttl_seconds:
                            stale_ids.append(state.subscription_id)

                    removed: list[_SubscriptionState] = []
                    for subscription_id in stale_ids:
                        if subscription_id not in self._subscriptions:
                            continue
                        state = self._subscriptions.pop(subscription_id)
                        state.update_event.set()
                        removed.append(state)

                for state in removed:
                    logger.warning("Subscription expired by TTL subscription_id=%s", state.subscription_id)
                    await self._release_all_nodes(state)
        except asyncio.CancelledError:
            return

    async def _handle_client_reconnect(self) -> None:
        async with self._lock:
            stale_bins = list(self._bins.values())
            self._bins.clear()
            affected_node_ids = [node_id for bin_ in stale_bins for node_id in bin_.node_handles]
            for node_id in affected_node_ids:
                monitor = self._node_monitors.get(node_id)
                if monitor is not None:
                    monitor.bin_id = None
                    monitor.mode = "idle"

        for bin_ in stale_bins:
            logger.info(
                "Reconfiguring native bin after reconnect bin_id=%s node_count=%d",
                bin_.bin_id,
                len(bin_.node_handles),
            )
            # The old ua_subscription is presumed dead after reconnect; best-effort cleanup.
            with suppress(Exception):
                await self._opcua_client.delete_subscription(bin_.ua_subscription)

            for node_id in sorted(bin_.node_handles):
                async with self._lock:
                    monitor = self._node_monitors.get(node_id)
                if monitor is None:
                    continue
                await self._start_node_monitor_runtime(monitor)

        async with self._lock:
            subscription_ids = list(self._subscriptions.keys())
        for subscription_id in subscription_ids:
            await self._update_subscription_mode(subscription_id)

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
        for state in self._subscriptions.values():
            state.update_event.set()

    async def close(self) -> None:
        # Signal all waiting SSE stream tasks to wake up and terminate *before*
        # any cleanup so that HTTP connections can be closed while Uvicorn is
        # still in its "Waiting for connections to close" phase.
        self._shutdown_event.set()
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

        datachange_task = self._datachange_task
        self._datachange_task = None
        if datachange_task is not None:
            datachange_task.cancel()
            try:
                await datachange_task
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

    async def create_subscription(self, client_id: str | None, display_name: str | None) -> SubscriptionDetail:
        self._ensure_cleanup_task()
        subscription_id = f"sub-{uuid4()}"
        state = _SubscriptionState(
            subscription_id=subscription_id,
            client_id=client_id,
            display_name=display_name,
            monitored_objects={},
            last_activity_monotonic=self._now_monotonic(),
        )
        async with self._lock:
            self._subscriptions[subscription_id] = state
        logger.info(
            "Subscription created subscription_id=%s client_id=%s display_name=%s",
            subscription_id,
            client_id,
            display_name,
        )
        return self._to_detail(state)

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
        return [self._to_detail(item) for item in values]

    async def get_subscription(self, subscription_id: str) -> SubscriptionDetail | None:
        async with self._lock:
            state = self._subscriptions.get(subscription_id)
            if state is None:
                return None
            return self._to_detail(state)

    async def delete_subscriptions(
        self,
        client_id: str | None,
        subscription_ids: list[str],
    ) -> list[SubscriptionDeleteResult]:
        removed: list[_SubscriptionState] = []
        results: list[SubscriptionDeleteResult] = []

        async with self._lock:
            for subscription_id in subscription_ids:
                state = self._subscriptions.get(subscription_id)
                if state is None:
                    results.append(
                        SubscriptionDeleteResult(
                            success=False,
                            subscription_id=subscription_id,
                            error={"code": 404, "message": "Subscription not found"},
                        )
                    )
                    continue
                if client_id is not None and state.client_id != client_id:
                    results.append(
                        SubscriptionDeleteResult(
                            success=False,
                            subscription_id=subscription_id,
                            error={"code": 404, "message": "Subscription not found"},
                        )
                    )
                    continue
                self._subscriptions.pop(subscription_id, None)
                removed.append(state)
                state.update_event.set()
                results.append(SubscriptionDeleteResult(success=True, subscription_id=subscription_id))

        for state in removed:
            await self._release_all_nodes(state)
            logger.info(
                "Subscription deleted subscription_id=%s client_id=%s monitored_nodes=%d",
                state.subscription_id,
                state.client_id,
                len(state.monitored_node_ids),
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
            state = self._subscriptions.get(subscription_id)
            if state is None or (client_id is not None and state.client_id != client_id):
                return False
            previous_node_ids = set(state.monitored_node_ids)
            previous_monitored_nodes = len(previous_node_ids)
            for element_id in element_ids:
                state.monitored_objects[element_id] = max_depth
            monitored_node_ids, node_to_element_id = self._resolve_monitored_node_ids(
                state.monitored_objects,
                model,
            )
            state.monitored_node_ids = monitored_node_ids
            state.node_to_element_id = node_to_element_id
            monitored_nodes_count = len(monitored_node_ids)
            monitored_objects_count = len(state.monitored_objects)
            state.last_values_by_node_id = {
                node_id: value
                for node_id, value in state.last_values_by_node_id.items()
                if node_id in monitored_node_ids
            }
            self._touch(state)

        await self._apply_monitored_node_diff(subscription_id, previous_node_ids, monitored_node_ids)
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
            state = self._subscriptions.get(subscription_id)
            if state is None or (client_id is not None and state.client_id != client_id):
                return False
            previous_node_ids = set(state.monitored_node_ids)
            previous_monitored_nodes = len(previous_node_ids)
            for element_id in element_ids:
                state.monitored_objects.pop(element_id, None)
            monitored_node_ids, node_to_element_id = self._resolve_monitored_node_ids(
                state.monitored_objects,
                model,
            )
            state.monitored_node_ids = monitored_node_ids
            state.node_to_element_id = node_to_element_id
            monitored_nodes_count = len(monitored_node_ids)
            monitored_objects_count = len(state.monitored_objects)
            state.last_values_by_node_id = {
                node_id: value
                for node_id, value in state.last_values_by_node_id.items()
                if node_id in monitored_node_ids
            }
            self._touch(state)

        await self._apply_monitored_node_diff(subscription_id, previous_node_ids, monitored_node_ids)
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
        acknowledge_sequence: int | None,
        allow_when_stream_active: bool = False,
    ) -> SubscriptionSyncResult | None:
        self._ensure_cleanup_task()
        should_refresh = False
        async with self._lock:
            state = self._subscriptions.get(subscription_id)
            if state is None or (client_id is not None and state.client_id != client_id):
                return None

            if state.stream_connected and not allow_when_stream_active:
                return SubscriptionSyncResult(updates=[], stream_active=True)

            if acknowledge_sequence == -1:
                state.updates.clear()
            elif isinstance(acknowledge_sequence, int):
                if 0 <= acknowledge_sequence <= state.sequence_number:
                    state.updates = [item for item in state.updates if item.sequence_number > acknowledge_sequence]

            should_refresh = acknowledge_sequence is None and not state.updates and bool(state.monitored_node_ids)

            self._touch(state)

        if should_refresh:
            await self._refresh_changed_values(subscription_id)

        async with self._lock:
            state = self._subscriptions.get(subscription_id)
            if state is None or (client_id is not None and state.client_id != client_id):
                return None

            result = SubscriptionSyncResult(
                updates=list(state.updates),
                queue_overflow=state.dropped_from_sequence is not None,
                dropped_from_sequence=state.dropped_from_sequence,
                dropped_to_sequence=state.dropped_to_sequence,
            )
            state.dropped_from_sequence = None
            state.dropped_to_sequence = None
            self._touch(state)
            return result

    async def activate_stream(self, client_id: str | None, subscription_id: str) -> int | None:
        self._ensure_cleanup_task()
        async with self._lock:
            state = self._subscriptions.get(subscription_id)
            if state is None or (client_id is not None and state.client_id != client_id):
                return None
            state.active_stream_generation += 1
            state.stream_connected = True
            self._touch(state)
            state.update_event.set()
            if _stream_debug_enabled():
                logger.info(
                    "Subscription service activate stream subscription_id=%s generation=%s monitored_nodes=%s mode=%s",
                    subscription_id,
                    state.active_stream_generation,
                    len(state.monitored_node_ids),
                    state.mode,
                )
            return state.active_stream_generation

    async def deactivate_stream(self, subscription_id: str, generation: int) -> None:
        async with self._lock:
            state = self._subscriptions.get(subscription_id)
            if state is None:
                return
            if state.active_stream_generation == generation:
                state.stream_connected = False
                self._touch(state)
                if _stream_debug_enabled():
                    logger.info(
                        "Subscription service deactivate stream subscription_id=%s generation=%s",
                        subscription_id,
                        generation,
                    )

    async def has_active_stream(self, client_id: str | None, subscription_id: str) -> bool | None:
        async with self._lock:
            state = self._subscriptions.get(subscription_id)
            if state is None or (client_id is not None and state.client_id != client_id):
                return None
            return state.stream_connected

    async def is_stream_active(self, subscription_id: str, generation: int) -> bool:
        async with self._lock:
            state = self._subscriptions.get(subscription_id)
            if state is None:
                return False
            return state.active_stream_generation == generation

    async def updates_after(self, subscription_id: str, after_sequence: int) -> list[SubscriptionUpdate] | None:
        async with self._lock:
            state = self._subscriptions.get(subscription_id)
            if state is None:
                return None
            return [item for item in state.updates if item.sequence_number > after_sequence]

    async def wait_for_updates(
        self,
        client_id: str | None,
        subscription_id: str,
        after_sequence: int,
        timeout_seconds: int = 15,
    ) -> list[SubscriptionUpdate] | None:
        self._ensure_cleanup_task()
        should_refresh_after_timeout = False
        refresh_interval_seconds: float | None = None
        refresh_timeout_threshold = 1
        async with self._lock:
            state = self._subscriptions.get(subscription_id)
            if state is None or (client_id is not None and state.client_id != client_id):
                return None

            self._touch(state)

            current = [item for item in state.updates if item.sequence_number > after_sequence]
            if current:
                state.native_timeout_count = 0
                return current

            state.update_event.clear()
            wait_event = state.update_event
            should_refresh_after_timeout = (
                state.mode == "native"
                and bool(state.monitored_node_ids)
                and self._native_timeout_refresh_mode != "strict"
            )
            refresh_interval_seconds = state.native_refresh_interval_seconds
            if should_refresh_after_timeout and self._native_timeout_refresh_mode == "adaptive":
                interval_seconds = refresh_interval_seconds or self._interval_seconds
                refresh_timeout_threshold = max(1, ceil(interval_seconds / max(0.001, timeout_seconds)))

        # Race: per-subscription update, service-wide shutdown, or wall-clock timeout.
        # Using asyncio.wait so we can also abort early on server shutdown without
        # replacing Uvicorn's own SIGINT/SIGTERM handling.
        _wakeup = asyncio.ensure_future(wait_event.wait())
        _abort = asyncio.ensure_future(self._shutdown_event.wait())
        done, _pending = await asyncio.wait(
            {_wakeup, _abort},
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for _task in (_wakeup, _abort):
            _task.cancel()
            with suppress(asyncio.CancelledError):
                await _task

        if self._shutdown_event.is_set():
            # Server is shutting down; tell the SSE generator to close gracefully.
            return None

        if not done:  # wall-clock timeout – same logic as the former except TimeoutError block
            should_execute_refresh = False
            timeout_count = 0
            async with self._lock:
                state = self._subscriptions.get(subscription_id)
                if state is None or (client_id is not None and state.client_id != client_id):
                    return None
                if should_refresh_after_timeout:
                    state.native_timeout_count += 1
                    timeout_count = state.native_timeout_count
                    if self._native_timeout_refresh_mode == "adaptive":
                        should_execute_refresh = timeout_count >= refresh_timeout_threshold
                    else:
                        should_execute_refresh = True

            if should_refresh_after_timeout:
                if _stream_debug_enabled() and self._native_timeout_refresh_mode == "adaptive":
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
                        state = self._subscriptions.get(subscription_id)
                        if state is None or (client_id is not None and state.client_id != client_id):
                            return None
                        state.native_timeout_count = 0
                if _stream_debug_enabled() and should_execute_refresh:
                    logger.info(
                        "Subscription service wait timeout refresh subscription_id=%s after_sequence=%s",
                        subscription_id,
                        after_sequence,
                    )
                if should_execute_refresh:
                    await self._refresh_changed_values(subscription_id)
            async with self._lock:
                state = self._subscriptions.get(subscription_id)
                if state is None or (client_id is not None and state.client_id != client_id):
                    return None
                self._touch(state)
                updates = [item for item in state.updates if item.sequence_number > after_sequence]
                if _stream_debug_enabled():
                    logger.info(
                        "Subscription service wait timeout result subscription_id=%s updates=%s after_sequence=%s",
                        subscription_id,
                        len(updates),
                        after_sequence,
                    )
                return updates

        async with self._lock:
            state = self._subscriptions.get(subscription_id)
            if state is None or (client_id is not None and state.client_id != client_id):
                return None
            state.native_timeout_count = 0
            self._touch(state)
            return [item for item in state.updates if item.sequence_number > after_sequence]

    async def handle_datachange(
        self,
        subscription_id: str,
        node_id: str,
        value: Any,
        client_handle: int | None = None,
    ) -> None:
        async with self._lock:
            state = self._subscriptions.get(subscription_id)
            if state is None:
                return

            resolved_node_id = node_id
            if client_handle is not None:
                mapped = state.handle_to_node_id.get(client_handle)
                if mapped is not None:
                    resolved_node_id = mapped

            if resolved_node_id not in state.monitored_node_ids:
                mapped_element = state.node_to_element_id.get(resolved_node_id)
                if mapped_element is None:
                    mapped_element = state.node_to_element_id.get(resolved_node_id.lower())
                if mapped_element is not None:
                    pass
                elif not state.monitored_node_ids:
                    return
                if len(state.monitored_node_ids) == 1:
                    resolved_node_id = next(iter(state.monitored_node_ids))
                elif mapped_element is None:
                    return

            self._append_update(state, resolved_node_id, value)
            logger.debug(
                "Subscription datachange callback subscription_id=%s mode=%s node_id=%s client_handle=%s",
                subscription_id,
                state.mode,
                resolved_node_id,
                client_handle,
            )
            state.native_timeout_count = 0
            # Removed self._touch(state) to ensure server notifications don't keep stale subscriptions alive.
            # Only client activity (sync, wait_for_updates) should refresh the TTL.

    async def _apply_monitored_node_diff(
        self,
        subscription_id: str,
        previous_node_ids: set[str],
        new_node_ids: set[str],
    ) -> None:
        """Reconcile a subscription's monitored nodes against the shared node registry."""
        removed = previous_node_ids - new_node_ids
        added = new_node_ids - previous_node_ids

        for node_id in removed:
            await self._release_node_monitor(node_id, subscription_id)
        for node_id in added:
            await self._acquire_node_monitor(node_id, subscription_id)

        await self._update_subscription_mode(subscription_id)
        await self._seed_initial_updates(subscription_id)

    async def _release_all_nodes(self, state: _SubscriptionState) -> None:
        for node_id in list(state.monitored_node_ids):
            await self._release_node_monitor(node_id, state.subscription_id)
        async with self._lock:
            state.mode = "idle"
            state.native_refresh_interval_seconds = None

    async def _update_subscription_mode(self, subscription_id: str) -> None:
        async with self._lock:
            state = self._subscriptions.get(subscription_id)
            if state is None:
                return
            if not state.monitored_node_ids:
                state.mode = "idle"
                state.native_refresh_interval_seconds = None
                return

            modes: list[str] = []
            native_intervals: list[float] = []
            for node_id in state.monitored_node_ids:
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
                state.mode = "native"
                state.native_refresh_interval_seconds = min(native_intervals) if native_intervals else None
            else:
                state.mode = "polling"
                state.native_refresh_interval_seconds = None

    async def _acquire_node_monitor(self, node_id: str, subscription_id: str) -> None:
        async with self._lock:
            monitor = self._node_monitors.get(node_id)
            if monitor is not None:
                monitor.subscriber_ids.add(subscription_id)
                return
            monitor = _NodeMonitor(node_id=node_id)
            monitor.subscriber_ids.add(subscription_id)
            self._node_monitors[node_id] = monitor

        await self._start_node_monitor_runtime(monitor)

    async def _release_node_monitor(self, node_id: str, subscription_id: str) -> None:
        async with self._lock:
            monitor = self._node_monitors.get(node_id)
            if monitor is None:
                return
            monitor.subscriber_ids.discard(subscription_id)
            should_stop = not monitor.subscriber_ids
            if should_stop:
                del self._node_monitors[node_id]

        if should_stop:
            await self._stop_node_monitor_runtime(monitor)

    async def _can_open_new_bin(self, caps: OpcUaSubscriptionCapabilities) -> bool:
        async with self._lock:
            bin_count = len(self._bins)
            total_monitored = sum(len(bin_.node_handles) for bin_ in self._bins.values())

        if caps.max_subscriptions is not None and bin_count + 1 > caps.max_subscriptions:
            return False
        if caps.max_subscriptions_per_session is not None and bin_count + 1 > caps.max_subscriptions_per_session:
            return False
        if caps.max_monitored_items is not None and total_monitored + 1 > caps.max_monitored_items:
            return False
        return True

    def _extract_revised_subscription_parameters(self, ua_subscription: Any) -> tuple[float | None, int | None]:
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

    def _compute_native_refresh_interval_seconds(self, ua_subscription: Any) -> float:
        publishing_interval_ms, max_keepalive_count = self._extract_revised_subscription_parameters(ua_subscription)
        if publishing_interval_ms is None or max_keepalive_count is None:
            fallback = min(
                self._native_timeout_refresh_max_seconds,
                self._interval_seconds * self._native_timeout_refresh_keepalives,
            )
            return max(0.1, fallback)

        keepalive_period_seconds = (publishing_interval_ms / 1000.0) * max_keepalive_count
        target_interval_seconds = keepalive_period_seconds * self._native_timeout_refresh_keepalives
        return max(0.1, min(self._native_timeout_refresh_max_seconds, target_interval_seconds))

    async def _start_node_monitor_runtime(self, monitor: _NodeMonitor) -> None:
        if self._now_monotonic() < self._native_backoff_until_monotonic:
            await self._start_polling_node_monitor(monitor)
            return

        caps = await self._opcua_client.get_subscription_capabilities()
        try:
            acquired = await self._acquire_native_bin_slot(monitor, caps)
        except Exception:
            logger.exception(
                "Native OPC UA monitor failed for node_id=%s; backing off native admissions for %.1fs",
                monitor.node_id,
                self._native_backoff_seconds,
            )
            self._native_backoff_until_monotonic = self._now_monotonic() + self._native_backoff_seconds
            acquired = False

        if not acquired:
            await self._start_polling_node_monitor(monitor)

    async def _acquire_native_bin_slot(self, monitor: _NodeMonitor, caps: OpcUaSubscriptionCapabilities) -> bool:
        """Pack monitor.node_id into an existing bin with spare capacity, or open a new one."""
        capacity = _min_positive(
            caps.max_monitored_items_per_call,
            caps.max_monitored_items_per_subscription,
        )
        if capacity is not None and capacity < 1:
            return False
        effective_capacity = capacity or _DEFAULT_BIN_CAPACITY

        async with self._lock:
            target_bin = next((b for b in self._bins.values() if len(b.node_handles) < b.capacity), None)

        if target_bin is not None:
            return await self._add_node_to_bin(monitor, target_bin)

        if not await self._can_open_new_bin(caps):
            return False

        return await self._open_new_bin(monitor, effective_capacity)

    async def _subscribe_single_node(self, ua_subscription: Any, node_id: str) -> int | None:
        handles = await self._opcua_client.subscribe_data_changes(ua_subscription, [node_id])
        if isinstance(handles, list):
            handle = handles[0] if handles else None
        else:
            handle = handles
        return _to_client_handle(handle)

    async def _open_new_bin(self, monitor: _NodeMonitor, capacity: int) -> bool:
        bin_id = f"bin-{uuid4()}"
        async with self._native_admission_semaphore:
            handler = _NodeDataChangeHandler(self, bin_id)
            ua_subscription = await self._opcua_client.create_datachange_subscription(
                publishing_interval_ms=float(self._interval_seconds * 1000),
                handler=handler,
            )
            handle = await self._subscribe_single_node(ua_subscription, monitor.node_id)
        if handle is None:
            with suppress(Exception):
                await self._opcua_client.delete_subscription(ua_subscription)
            return False

        async with self._lock:
            live = self._node_monitors.get(monitor.node_id)
            if live is None or live is not monitor:
                # Monitor was released concurrently (no subscribers left); undo the create.
                await self._opcua_client.delete_subscription(ua_subscription)
                return False
            new_bin = _ManagedBin(bin_id=bin_id, ua_subscription=ua_subscription, capacity=capacity)
            new_bin.node_handles[monitor.node_id] = handle
            new_bin.native_refresh_interval_seconds = self._compute_native_refresh_interval_seconds(ua_subscription)
            self._bins[bin_id] = new_bin
            live.bin_id = bin_id
            live.mode = "native"
            live.native_refresh_interval_seconds = new_bin.native_refresh_interval_seconds
        logger.info(
            "Node monitor bin opened bin_id=%s node_id=%s capacity=%d",
            bin_id,
            monitor.node_id,
            capacity,
        )
        return True

    async def _add_node_to_bin(self, monitor: _NodeMonitor, target_bin: _ManagedBin) -> bool:
        async with self._native_admission_semaphore:
            handle = await self._subscribe_single_node(target_bin.ua_subscription, monitor.node_id)
        if handle is None:
            return False

        async with self._lock:
            live = self._node_monitors.get(monitor.node_id)
            live_bin = self._bins.get(target_bin.bin_id)
            if live is None or live is not monitor or live_bin is None:
                # Monitor released concurrently, or the bin was reclaimed/closed under us.
                with suppress(Exception):
                    await self._opcua_client.unsubscribe_data_changes(target_bin.ua_subscription, [handle])
                return False
            live_bin.node_handles[monitor.node_id] = handle
            live.bin_id = live_bin.bin_id
            live.mode = "native"
            live.native_refresh_interval_seconds = live_bin.native_refresh_interval_seconds
        logger.info(
            "Node monitor added to bin bin_id=%s node_id=%s size=%d/%d",
            target_bin.bin_id,
            monitor.node_id,
            len(target_bin.node_handles),
            target_bin.capacity,
        )
        return True

    async def _start_polling_node_monitor(self, monitor: _NodeMonitor) -> None:
        async with self._lock:
            live = self._node_monitors.get(monitor.node_id)
            if live is None or live is not monitor:
                return
            live.mode = "polling"
            live.bin_id = None
            live.native_refresh_interval_seconds = None
        logger.info(
            "Node monitor runtime started node_id=%s mode=polling interval_s=%.3f",
            monitor.node_id,
            self._interval_seconds,
        )

    async def _stop_node_monitor_runtime(self, monitor: _NodeMonitor) -> None:
        bin_id = monitor.bin_id
        monitor.bin_id = None
        monitor.mode = "idle"
        monitor.native_refresh_interval_seconds = None

        if bin_id is None:
            return

        async with self._lock:
            target_bin = self._bins.get(bin_id)
            handle = target_bin.node_handles.pop(monitor.node_id, None) if target_bin is not None else None
            bin_now_empty = target_bin is not None and not target_bin.node_handles
            if bin_now_empty:
                del self._bins[bin_id]

        if target_bin is None:
            return

        if bin_now_empty:
            try:
                await self._opcua_client.delete_subscription(target_bin.ua_subscription)
            except Exception:
                logger.debug(
                    "Ignoring delete failure for empty bin bin_id=%s",
                    bin_id,
                    exc_info=True,
                )
            logger.info("Node monitor bin reclaimed bin_id=%s", bin_id)
        elif handle is not None:
            try:
                await self._opcua_client.unsubscribe_data_changes(target_bin.ua_subscription, [handle])
            except Exception:
                logger.debug(
                    "Ignoring unsubscribe failure bin_id=%s node_id=%s",
                    bin_id,
                    monitor.node_id,
                    exc_info=True,
                )

    async def _shared_polling_loop(self) -> None:
        """Single batched read loop covering every node currently in polling mode."""
        try:
            while True:
                async with self._lock:
                    node_ids = sorted(
                        node_id for node_id, monitor in self._node_monitors.items() if monitor.mode == "polling"
                    )

                if node_ids:
                    try:
                        values = await self._opcua_client.read_values(node_ids)
                    except Exception:
                        logger.exception("Shared polling read failed node_count=%d", len(node_ids))
                    else:
                        for node_id, value in zip(node_ids, values, strict=False):
                            await self._fanout_node_value(node_id, value)

                await asyncio.sleep(self._interval_seconds)
        except asyncio.CancelledError:
            return

    async def _seed_initial_updates(self, subscription_id: str) -> None:
        if not self._seed_initial_values:
            return

        await self._refresh_changed_values(subscription_id)

    async def _refresh_changed_values(self, subscription_id: str) -> None:
        node_ids: list[str] = []

        async with self._lock:
            state = self._subscriptions.get(subscription_id)
            if state is None or state.mode == "idle":
                return
            node_ids = sorted(state.monitored_node_ids)

        if not node_ids:
            return

        try:
            values = await self._opcua_client.read_values(node_ids)
        except Exception:
            logger.exception("Initial subscription snapshot failed", extra={"subscription_id": subscription_id})
            return

        async with self._lock:
            state = self._subscriptions.get(subscription_id)
            if state is None:
                return
            for node_id, value in zip(node_ids, values, strict=False):
                if node_id not in state.monitored_node_ids:
                    continue
                self._append_update(state, node_id, value)
            self._touch(state)

    def _append_update(self, state: _SubscriptionState, node_id: str, value: Any) -> None:
        has_current = node_id in state.last_values_by_node_id
        current = state.last_values_by_node_id.get(node_id)
        if has_current and current == value:
            return

        state.last_values_by_node_id[node_id] = value
        state.sequence_number += 1
        element_id = state.node_to_element_id.get(node_id)
        if element_id is None:
            element_id = state.node_to_element_id.get(node_id.lower(), node_id)
        if element_id == node_id and len(state.monitored_objects) == 1:
            # Compatibility fallback: if native callback mapping misses for a
            # single registered object, emit that object id so consumers can
            # still match updates to the active subscription row.
            element_id = next(iter(state.monitored_objects.keys()))
        quality = "GoodNoData" if value is None else "Good"
        if len(state.updates) >= self._max_updates_per_subscription:
            dropped = state.updates.pop(0)
            if state.dropped_from_sequence is None:
                state.dropped_from_sequence = dropped.sequence_number
            state.dropped_to_sequence = dropped.sequence_number
        state.updates.append(
            SubscriptionUpdate(
                sequence_number=state.sequence_number,
                element_id=element_id,
                node_id=node_id,
                value=value,
                quality=quality,
                timestamp=_format_utc_timestamp(datetime.now(timezone.utc)),
            )
        )
        state.update_event.set()

    def _to_detail(self, state: _SubscriptionState) -> SubscriptionDetail:
        monitored = [
            {"elementId": element_id, "maxDepth": max_depth}
            for element_id, max_depth in state.monitored_objects.items()
        ]
        return SubscriptionDetail(
            subscription_id=state.subscription_id,
            client_id=state.client_id,
            display_name=state.display_name,
            monitored_objects=monitored,
            mode=state.mode,
        )

    def _resolve_monitored_node_ids(
        self,
        monitored_objects: dict[str, int],
        model: BuildResult,
    ) -> tuple[set[str], dict[str, str]]:
        source_index = {item.source_node_id: item for item in model.nodes_by_id.values()}
        node_ids: set[str] = set()
        node_to_element_id: dict[str, str] = {}

        for element_id, max_depth in monitored_objects.items():
            node = model.nodes_by_id.get(element_id)
            if node is None:
                node = source_index.get(element_id)
            if node is None:
                node_ids.add(element_id)
                node_to_element_id[element_id] = element_id
                node_to_element_id[element_id.lower()] = element_id
                continue

            mappings = self._collect_property_source_mappings(model, node, max_depth=max_depth)
            node_ids.update(mappings.keys())
            node_to_element_id.update(mappings)
            node_to_element_id.update({key.lower(): value for key, value in mappings.items()})

        return node_ids, node_to_element_id

    def _collect_property_source_mappings(
        self,
        model: BuildResult,
        root: ModelNode,
        max_depth: int,
    ) -> dict[str, str]:
        if root.kind == "property":
            return {root.source_node_id: root.id}

        result: dict[str, str] = {}
        depth_limit = max(0, max_depth)
        queue: list[tuple[str, int]] = [(root.id, 0)]

        while queue:
            node_id, depth = queue.pop(0)
            node = model.nodes_by_id.get(node_id)
            if node is None:
                continue

            if node.kind == "property":
                result[node.source_node_id] = node.id
                continue

            if depth_limit != 0 and depth >= depth_limit:
                continue

            for child_id in model.children_by_id.get(node.id, []):
                queue.append((child_id, depth + 1))

        return result


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


def _format_utc_timestamp(value: datetime) -> str:
    normalized = value
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
