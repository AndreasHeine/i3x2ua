"""Shared data structures for the subscription service.

The service owns subscription state, node monitors and native bins; these
dataclasses are the vocabulary shared between the service, the monitoring
runtime (``monitoring.py``) and the REST layer. Tests and monkeypatching
target this module for them.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from i3x_server.config.settings import get_settings
from i3x_server.schemas.opcua_value import decode_data_value


@dataclass(frozen=True, slots=True)
class _ValueSignal:
    """Server-side quality/timestamp carried alongside a value.

    ``None`` fields mean the upstream gave no signal, so the caller decides what to report
    instead of inventing ``Good`` and a gateway timestamp for every update.
    """

    quality: str | None = None
    timestamp: str | None = None


_NO_SIGNAL = _ValueSignal()


def _signal_from_data_value(data_value: Any) -> _ValueSignal:
    _payload, quality, timestamp = decode_data_value(data_value)
    return _ValueSignal(quality=quality, timestamp=timestamp)


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
    # Per-object node_id-to-elementId mappings (canonical keys only), added and removed
    # incrementally on register/unregister instead of re-resolving every object.
    object_mappings: dict[str, dict[str, str]] = field(default_factory=dict)
    # Model revision the mappings were resolved against; a refresh forces a full rebuild.
    mapping_model_token: str | None = None
    monitored_node_ids: set[str] = field(default_factory=set)
    applied_node_ids: set[str] = field(default_factory=set)
    node_to_element_id: dict[str, str] = field(default_factory=dict)
    last_values_by_node_id: dict[str, Any] = field(default_factory=dict)
    last_quality_by_node_id: dict[str, str] = field(default_factory=dict)
    # Bounded update buffer. A deque drops the oldest entry in O(1); ``list.pop(0)`` shifted every
    # remaining entry on each eviction, so a saturated subscription re-wrote its whole buffer per
    # update. Delivery walks it from the tail, which makes cursor reads O(delivered) not O(buffered).
    updates: deque[SubscriptionUpdate] = field(default_factory=deque)
    sequence_number: int = 0
    mode: str = "idle"
    update_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Per-subscription delivery lock. The registry lock guards membership, mappings, and monitor
    # bookkeeping; the update buffer, sequence counters, and drop markers change under this lock, so
    # fan-out ticks of different subscriptions no longer serialize behind one global lock.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
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
    # Node ids reserved by admissions whose network round-trip is still in flight, in reservation
    # order. They count towards the bin capacity like real handles, and the admission that creates
    # the subscription batches them into one CreateMonitoredItems call instead of one RPC per node.
    pending_nodes: list[str] = field(default_factory=list)
    # Set once ``ua_subscription`` exists. A bin registered before its create returned is a
    # placeholder: concurrent admissions reserve slots in it and wait here instead of opening more bins.
    ready: asyncio.Event = field(default_factory=asyncio.Event)
