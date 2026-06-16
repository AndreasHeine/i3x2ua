from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from i3x_server.domain.ports.opcua import OpcUaClientProtocol, OpcUaSubscriptionCapabilities
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult

logger = logging.getLogger(__name__)


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
class _SubscriptionRuntime:
    ua_subscription: Any | None = None
    polling_task: asyncio.Task[None] | None = None


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
    runtime: _SubscriptionRuntime = field(default_factory=_SubscriptionRuntime)
    update_event: asyncio.Event = field(default_factory=asyncio.Event)
    active_stream_generation: int = 0
    stream_connected: bool = False
    dropped_from_sequence: int | None = None
    dropped_to_sequence: int | None = None
    last_activity_monotonic: float = 0.0


class _DataChangeHandler:
    def __init__(self, service: SubscriptionService, subscription_id: str) -> None:
        self._service = service
        self._subscription_id = subscription_id

    def datachange_notification(self, node: Any, val: Any, data: Any) -> None:
        node_id = node.nodeid.to_string()
        client_handle: int | None = None
        monitored_item = getattr(data, "monitored_item", None)
        if monitored_item is not None:
            raw_handle = getattr(monitored_item, "ClientHandle", None)
            if isinstance(raw_handle, int):
                client_handle = raw_handle

        asyncio.create_task(self._service.handle_datachange(self._subscription_id, node_id, val, client_handle))

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
    ) -> None:
        self._opcua_client = opcua_client
        self._interval_seconds = max(0.1, float(interval_seconds))
        self._max_updates_per_subscription = max(1, max_updates_per_subscription)
        self._ttl_seconds = max(1, ttl_seconds)
        self._seed_initial_values = seed_initial_values
        self._lock = asyncio.Lock()
        self._subscriptions: dict[str, _SubscriptionState] = {}
        self._cleanup_task: asyncio.Task[None] | None = None
        self._opcua_client.add_reconnect_listener(self._handle_client_reconnect)

    def _now_monotonic(self) -> float:
        return asyncio.get_running_loop().time()

    def _touch(self, state: _SubscriptionState) -> None:
        state.last_activity_monotonic = self._now_monotonic()

    def _ensure_cleanup_task(self) -> None:
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(max(0.1, self._interval_seconds))
                stale_ids: list[str] = []
                now = self._now_monotonic()
                async with self._lock:
                    for state in self._subscriptions.values():
                        if state.stream_connected:
                            continue
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
                    logger.info("Subscription expired by TTL subscription_id=%s", state.subscription_id)
                    await self._stop_runtime(state)
        except asyncio.CancelledError:
            return

    async def _handle_client_reconnect(self) -> None:
        async with self._lock:
            candidates = [
                item.subscription_id
                for item in self._subscriptions.values()
                if item.mode == "native" and item.monitored_node_ids
            ]

        for subscription_id in candidates:
            logger.info("Reconfiguring native subscription after reconnect subscription_id=%s", subscription_id)
            await self._reconfigure_runtime(subscription_id)

    async def close(self) -> None:
        async with self._lock:
            subscriptions = list(self._subscriptions.values())

        for subscription in subscriptions:
            await self._stop_runtime(subscription)

        cleanup_task = self._cleanup_task
        self._cleanup_task = None
        if cleanup_task is not None:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass

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
            await self._stop_runtime(state)

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
        async with self._lock:
            state = self._subscriptions.get(subscription_id)
            if state is None or (client_id is not None and state.client_id != client_id):
                return False
            for element_id in element_ids:
                state.monitored_objects[element_id] = max_depth
            monitored_node_ids, node_to_element_id = self._resolve_monitored_node_ids(
                state.monitored_objects,
                model,
            )
            state.monitored_node_ids = monitored_node_ids
            state.node_to_element_id = node_to_element_id
            state.last_values_by_node_id = {
                node_id: value
                for node_id, value in state.last_values_by_node_id.items()
                if node_id in monitored_node_ids
            }
            self._touch(state)

        await self._reconfigure_runtime(subscription_id)
        return True

    async def unregister_items(
        self,
        client_id: str | None,
        subscription_id: str,
        element_ids: list[str],
        model: BuildResult,
    ) -> bool:
        self._ensure_cleanup_task()
        async with self._lock:
            state = self._subscriptions.get(subscription_id)
            if state is None or (client_id is not None and state.client_id != client_id):
                return False
            for element_id in element_ids:
                state.monitored_objects.pop(element_id, None)
            monitored_node_ids, node_to_element_id = self._resolve_monitored_node_ids(
                state.monitored_objects,
                model,
            )
            state.monitored_node_ids = monitored_node_ids
            state.node_to_element_id = node_to_element_id
            state.last_values_by_node_id = {
                node_id: value
                for node_id, value in state.last_values_by_node_id.items()
                if node_id in monitored_node_ids
            }
            self._touch(state)

        await self._reconfigure_runtime(subscription_id)
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
            return state.active_stream_generation

    async def deactivate_stream(self, subscription_id: str, generation: int) -> None:
        async with self._lock:
            state = self._subscriptions.get(subscription_id)
            if state is None:
                return
            if state.active_stream_generation == generation:
                state.stream_connected = False
                self._touch(state)

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
        async with self._lock:
            state = self._subscriptions.get(subscription_id)
            if state is None or (client_id is not None and state.client_id != client_id):
                return None

            self._touch(state)

            current = [item for item in state.updates if item.sequence_number > after_sequence]
            if current:
                return current

            state.update_event.clear()
            wait_event = state.update_event

        try:
            await asyncio.wait_for(wait_event.wait(), timeout=timeout_seconds)
        except TimeoutError:
            return []

        async with self._lock:
            state = self._subscriptions.get(subscription_id)
            if state is None or (client_id is not None and state.client_id != client_id):
                return None
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

            self._append_update(state, resolved_node_id, value)
            self._touch(state)

    async def _reconfigure_runtime(self, subscription_id: str) -> None:
        async with self._lock:
            state = self._subscriptions.get(subscription_id)

        if state is None:
            return

        await self._stop_runtime(state)

        if not state.monitored_node_ids:
            async with self._lock:
                state.mode = "idle"
            return

        caps = await self._opcua_client.get_subscription_capabilities()
        should_poll = await self._must_use_polling(state, caps)

        if should_poll:
            await self._start_polling(state)
            await self._seed_initial_updates(subscription_id)
            return

        try:
            await self._start_native_subscription(state)
            await self._seed_initial_updates(subscription_id)
        except Exception:
            logger.exception(
                "Native OPC UA subscription failed; switching to polling",
                extra={"subscription_id": state.subscription_id},
            )
            await self._start_polling(state)
            await self._seed_initial_updates(subscription_id)

    async def _must_use_polling(self, state: _SubscriptionState, caps: OpcUaSubscriptionCapabilities) -> bool:
        node_count = len(state.monitored_node_ids)
        max_per_subscription = _min_positive(
            caps.max_monitored_items_per_call,
            caps.max_monitored_items_per_subscription,
        )
        if max_per_subscription is not None and node_count > max_per_subscription:
            return True

        async with self._lock:
            native_subscriptions = [item for item in self._subscriptions.values() if item.mode == "native"]
            native_count = len(native_subscriptions)
            native_monitored = sum(len(item.monitored_node_ids) for item in native_subscriptions)

        if caps.max_subscriptions is not None and native_count + 1 > caps.max_subscriptions:
            return True
        if caps.max_subscriptions_per_session is not None and native_count + 1 > caps.max_subscriptions_per_session:
            return True
        if caps.max_monitored_items is not None and native_monitored + node_count > caps.max_monitored_items:
            return True
        return False

    async def _start_native_subscription(self, state: _SubscriptionState) -> None:
        handler = _DataChangeHandler(self, state.subscription_id)
        sorted_node_ids = sorted(state.monitored_node_ids)
        ua_subscription = await self._opcua_client.create_datachange_subscription(
            publishing_interval_ms=float(self._interval_seconds * 1000),
            handler=handler,
        )
        handles = await self._opcua_client.subscribe_data_changes(ua_subscription, sorted_node_ids)
        handle_to_node_id: dict[int, str] = {}
        if isinstance(handles, list):
            for handle, node_id in zip(handles, sorted_node_ids, strict=False):
                if isinstance(handle, int):
                    handle_to_node_id[handle] = node_id

        async with self._lock:
            live = self._subscriptions.get(state.subscription_id)
            if live is None:
                await self._opcua_client.delete_subscription(ua_subscription)
                return
            live.runtime.ua_subscription = ua_subscription
            live.handle_to_node_id = handle_to_node_id
            live.mode = "native"

    async def _start_polling(self, state: _SubscriptionState) -> None:
        task = asyncio.create_task(self._polling_loop(state.subscription_id))
        async with self._lock:
            live = self._subscriptions.get(state.subscription_id)
            if live is None:
                task.cancel()
                return
            live.runtime.polling_task = task
            live.mode = "polling"

    async def _stop_runtime(self, state: _SubscriptionState) -> None:
        polling_task = state.runtime.polling_task
        ua_subscription = state.runtime.ua_subscription
        state.runtime.polling_task = None
        state.runtime.ua_subscription = None
        state.handle_to_node_id = {}
        state.mode = "idle"

        if polling_task is not None:
            polling_task.cancel()
            try:
                await polling_task
            except asyncio.CancelledError:
                pass

        if ua_subscription is not None:
            try:
                await self._opcua_client.delete_subscription(ua_subscription)
            except Exception:
                logger.debug(
                    "Ignoring delete failure for stale OPC UA subscription subscription_id=%s",
                    state.subscription_id,
                    exc_info=True,
                )

    async def _polling_loop(self, subscription_id: str) -> None:
        try:
            while True:
                async with self._lock:
                    state = self._subscriptions.get(subscription_id)
                    if state is None or state.mode != "polling":
                        return
                    node_ids = sorted(state.monitored_node_ids)

                if node_ids:
                    try:
                        values = await self._opcua_client.read_values(node_ids)
                    except Exception:
                        logger.exception("Polling read failed", extra={"subscription_id": subscription_id})
                    else:
                        async with self._lock:
                            state = self._subscriptions.get(subscription_id)
                            if state is None or state.mode != "polling":
                                return
                            for node_id, value in zip(node_ids, values, strict=False):
                                self._append_update(state, node_id, value)

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


def _format_utc_timestamp(value: datetime) -> str:
    normalized = value
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
