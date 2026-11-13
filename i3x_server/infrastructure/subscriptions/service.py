"""Subscription service: i3X subscription state over shared OPC UA node monitors.

The shared vocabulary (updates, sync results, subscription/monitor/bin state) lives in
``state.py``; the OPC UA monitoring runtime (native bin packing, the shared polling loop,
reconnect resync) lives in ``monitoring.py``; the elementId-to-source-node mapping lives in
``mapping.py``. The behaviour is split by responsibility: ``registry.py`` (lifecycle, CRUD,
TTL expiry, shutdown), ``items.py`` (monitored-item registration and sync),
``monitored_nodes.py`` (shared node monitors and value refresh), ``streams.py`` (SSE
activation and the wait cycle), and ``updates.py`` (shared datachange fan-out and update
buffering).

This module owns the constructor - the attribute layout every module and ``monitoring``
reads - plus thin delegating methods, so ``SubscriptionService`` stays the single entry
point for routes, tests, and the monitoring runtime. Implementation lives in the source
modules; monkeypatching a method here affects every path that reaches it through the
service.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from i3x_server.domain.ports.opcua import OpcUaClientProtocol, OpcUaSubscriptionCapabilities
from i3x_server.infrastructure.subscriptions import (
    items,
    monitored_nodes,
    monitoring,
    registry,
    state,
    streams,
    updates,
)
from i3x_server.infrastructure.subscriptions.state import (
    SubscriptionDeleteResult,
    SubscriptionDetail,
    SubscriptionSyncResult,
    SubscriptionUpdate,
)
from i3x_server.schemas.i3x import ModelNode
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
        registry._remember_event_loop(self)

    def _now_monotonic(self) -> float:
        return registry._now_monotonic(self)

    def _touch(self, state_obj: state._SubscriptionState) -> None:
        registry._touch(self, state_obj)

    def _ensure_cleanup_task(self) -> None:
        registry._ensure_cleanup_task(self)

    async def _cleanup_loop(self) -> None:
        await registry._cleanup_loop(self)

    async def _expire_subscriptions(self) -> None:
        await registry._expire_subscriptions(self)

    async def _handle_client_reconnect(self) -> None:
        await registry._handle_client_reconnect(self)

    def initiate_shutdown(self) -> None:
        """Signal all active SSE streams to close immediately (see ``registry._initiate_shutdown``)."""
        registry._initiate_shutdown(self)

    async def close(self) -> None:
        """Tear down the runtime: streams, node monitors, background tasks, reconnect listener."""
        await registry._close(self)

    async def create_subscription(self, client_id: str | None, display_name: str | None = None) -> SubscriptionDetail:
        return await registry._create_subscription(self, client_id, display_name)

    async def list_subscriptions(
        self,
        client_id: str | None,
        subscription_ids: list[str] | None = None,
    ) -> list[SubscriptionDetail]:
        return await registry._list_subscriptions(self, client_id, subscription_ids)

    async def delete_subscriptions(
        self,
        client_id: str | None,
        subscription_ids: list[str],
    ) -> list[SubscriptionDeleteResult]:
        return await registry._delete_subscriptions(self, client_id, subscription_ids)

    async def register_items(
        self,
        client_id: str | None,
        subscription_id: str,
        element_ids: list[str],
        max_depth: int,
        model: BuildResult,
    ) -> bool:
        return await items._register_items(self, client_id, subscription_id, element_ids, max_depth, model)

    async def unregister_items(
        self,
        client_id: str | None,
        subscription_id: str,
        element_ids: list[str],
        model: BuildResult,
    ) -> bool:
        return await items._unregister_items(self, client_id, subscription_id, element_ids, model)

    async def sync(
        self,
        client_id: str | None,
        subscription_id: str,
        acknowledge_sequence: int | None = None,
        allow_when_stream_active: bool = False,
    ) -> SubscriptionSyncResult | None:
        return await items._sync(self, client_id, subscription_id, acknowledge_sequence, allow_when_stream_active)

    async def activate_stream(self, client_id: str | None, subscription_id: str) -> int | None:
        return await streams._activate_stream(self, client_id, subscription_id)

    async def deactivate_stream(self, subscription_id: str, stream_generation: int) -> None:
        await streams._deactivate_stream(self, subscription_id, stream_generation)

    async def is_stream_active(self, subscription_id: str, stream_generation: int) -> bool:
        return await streams._is_stream_active(self, subscription_id, stream_generation)

    async def wait_for_updates(
        self,
        client_id: str | None,
        subscription_id: str,
        after_sequence: int,
        timeout_seconds: float = 15.0,
    ) -> list[SubscriptionUpdate] | None:
        return await streams._wait_for_updates(self, client_id, subscription_id, after_sequence, timeout_seconds)

    async def handle_datachange(
        self,
        subscription_id: str,
        node_id: str,
        value: Any,
        signal: state._ValueSignal = state._NO_SIGNAL,
    ) -> None:
        await monitored_nodes._handle_datachange(self, subscription_id, node_id, value, signal)

    async def _apply_monitored_node_diff(self, subscription_id: str) -> None:
        """Reconcile the shared node registry against the subscription's current desired set."""
        await items._apply_monitored_node_diff(self, subscription_id)

    async def _release_all_nodes(self, subscription_state: state._SubscriptionState) -> None:
        await items._release_all_nodes(self, subscription_state)

    async def _update_subscription_mode(self, subscription_id: str) -> None:
        await monitored_nodes._update_subscription_mode(self, subscription_id)

    async def _acquire_node_monitor(self, node_id: str, subscription_id: str) -> None:
        await monitored_nodes._acquire_node_monitor(self, node_id, subscription_id)

    async def _release_node_monitor(self, node_id: str, subscription_id: str) -> None:
        await monitored_nodes._release_node_monitor(self, node_id, subscription_id)

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
        await monitored_nodes._seed_initial_updates(self, subscription_id)

    async def _refresh_changed_values(self, subscription_id: str) -> None:
        await monitored_nodes._refresh_changed_values(self, subscription_id)

    def _resolve_target_node(self, subscription_state: state._SubscriptionState, node_id: str) -> str | None:
        """Resolve the monitored node an incoming notification refers to (registry lock)."""
        return items._resolve_target_node(subscription_state, node_id)

    def _resolve_element_id(self, subscription_state: state._SubscriptionState, node_id: str) -> str:
        """Map a node id to the element id reported in updates (registry lock)."""
        return items._resolve_element_id(subscription_state, node_id)

    def _updates_after(
        self, subscription_state: state._SubscriptionState, after_sequence: int
    ) -> list[SubscriptionUpdate]:
        """Buffered updates with a sequence number greater than ``after_sequence``."""
        return updates.updates_after(subscription_state, after_sequence)

    def _append_update(
        self,
        subscription_state: state._SubscriptionState,
        node_id: str,
        value: Any,
        signal: state._ValueSignal = state._NO_SIGNAL,
        element_id: str | None = None,
    ) -> None:
        """Append one update to a subscription's buffer under ``subscription_state.lock``."""
        updates.append_update(self, subscription_state, node_id, value, signal, element_id=element_id)

    def _to_detail(self, subscription_state: state._SubscriptionState) -> SubscriptionDetail:
        return registry._to_detail(self, subscription_state)

    def _source_index(self, token: str, model: BuildResult) -> dict[str, ModelNode]:
        """Model source-node lookup cached per model revision token."""
        return items._source_index(self, token, model)

    @staticmethod
    def _merge_object_mappings(object_mappings: dict[str, dict[str, str]]) -> tuple[set[str], dict[str, str]]:
        """Fold per-object mappings into the monitored node set and the case-folded lookup."""
        return items._merge_object_mappings(object_mappings)

    def _resolve_monitored_node_ids(
        self,
        monitored_objects: dict[str, int],
        model: BuildResult,
        max_nodes: int | None = None,
    ) -> tuple[set[str], dict[str, str]]:
        # Kept as the service-facing entry point; the mapping lives in ``mapping.py``.
        # ``max_nodes`` defaults to the service's subtree budget.
        return items._resolve_monitored_node_ids(self, monitored_objects, model, max_nodes)

    def _collect_property_source_mappings(
        self,
        model: BuildResult,
        root: ModelNode,
        max_depth: int,
        max_nodes: int | None = None,
    ) -> dict[str, str]:
        # Kept as the service-facing entry point; the mapping lives in ``mapping.py``.
        # ``max_nodes`` defaults to the service's subtree budget.
        return items._collect_property_source_mappings(self, model, root, max_depth, max_nodes)

    def schedule_node_datachange(
        self,
        node_id: str,
        value: Any,
        signal: state._ValueSignal = state._NO_SIGNAL,
    ) -> None:
        updates.schedule_node_datachange(self, node_id, value, signal)

    def _enqueue_node_datachange(
        self,
        node_id: str,
        value: Any,
        signal: state._ValueSignal = state._NO_SIGNAL,
    ) -> None:
        updates._enqueue_node_datachange(self, node_id, value, signal)

    async def _node_datachange_loop(self) -> None:
        await updates._node_datachange_loop(self)

    async def _fanout_node_value(
        self,
        node_id: str,
        value: Any,
        signal: state._ValueSignal = state._NO_SIGNAL,
    ) -> None:
        """Deliver one node-level value change to every subscription monitoring it."""
        await updates._fanout_node_value(self, node_id, value, signal)
