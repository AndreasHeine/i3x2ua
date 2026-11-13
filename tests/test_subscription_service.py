from __future__ import annotations

import asyncio
import gc
import time
import weakref
from contextlib import suppress
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest
from asyncua import ua
from asyncua.ua.status_codes import StatusCodes
from asyncua.ua.uaerrors import UaStatusCodeError

from i3x_server.domain.errors import AdmissionLimitError
from i3x_server.domain.ports.opcua import DataValueDTO, OpcUaValue
from i3x_server.infrastructure.opcua.client import (
    OpcUaClientProtocol,
    OpcUaSubscriptionCapabilities,
)
from i3x_server.infrastructure.subscriptions import mapping
from i3x_server.infrastructure.subscriptions.monitoring import (
    _add_node_to_bin,
    _capped_limit,
    _min_positive,
    _open_new_bin,
)
from i3x_server.infrastructure.subscriptions.service import SubscriptionService, SubscriptionUpdate
from i3x_server.infrastructure.subscriptions.state import (
    _ManagedBin,
    _NodeMonitor,
    _SubscriptionState,
    _ValueSignal,
)
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.opcua_value import normalize_opcua_value, quality_from_status_code
from i3x_server.schemas.state import BuildResult


def _signed_dto(value: Any, severity: int, timestamp: datetime) -> DataValueDTO:
    """DTO for a server-signed value: numeric severity code plus the adapter-decoded quality."""
    return DataValueDTO(
        value=OpcUaValue(value=value),
        status_code=severity,
        quality=quality_from_status_code(SimpleNamespace(value=severity)),
        source_timestamp=timestamp,
    )


class FakeOpcUaClient:
    def __init__(self) -> None:
        self.listeners: list[Any] = []
        self.removed_listeners: list[Any] = []
        self.deleted_subscriptions: list[Any] = []
        self.read_values_calls = 0
        self.read_data_values_calls = 0
        self.create_subscription_calls = 0
        self.subscribe_calls = 0
        self.unsubscribed_calls: list[tuple[Any, list[int]]] = []
        # Handlers handed to create_datachange_subscription, so tests can fire notifications.
        self.handlers: list[Any] = []
        # node_id -> DTO to serve instead of the generated Good value.
        self.data_value_overrides: dict[str, DataValueDTO] = {}
        # id(ua_subscription) -> handles asyncua reports after a reconnect (None = subscription lost).
        self.restored_handles: dict[int, dict[str, int] | None] = {}

    def add_reconnect_listener(self, listener: Any) -> None:
        self.listeners.append(listener)

    def remove_reconnect_listener(self, listener: Any) -> None:
        self.removed_listeners.append(listener)
        if listener in self.listeners:
            self.listeners.remove(listener)

    async def get_subscription_capabilities(self) -> Any:
        return SimpleNamespace(
            max_monitored_items_per_call=100,
            max_subscriptions=100,
            max_monitored_items=100,
            max_subscriptions_per_session=100,
            max_monitored_items_per_subscription=100,
        )

    async def create_datachange_subscription(self, publishing_interval_ms: float, handler: Any) -> Any:
        del publishing_interval_ms
        self.handlers.append(handler)
        self.create_subscription_calls += 1
        return SimpleNamespace(id=f"ua-sub-{self.create_subscription_calls}")

    async def subscribe_data_changes(self, subscription: Any, node_ids: list[str]) -> int | list[int]:
        del subscription
        handles: list[int] = []
        for _ in node_ids:
            self.subscribe_calls += 1
            handles.append(self.subscribe_calls)
        return handles

    async def unsubscribe_data_changes(self, subscription: Any, handles: list[int]) -> None:
        self.unsubscribed_calls.append((subscription, list(handles)))

    async def delete_subscription(self, subscription: Any) -> None:
        self.deleted_subscriptions.append(subscription)

    def get_monitored_item_handles(self, subscription: Any, node_ids: list[str]) -> dict[str, int] | None:
        del node_ids
        return self.restored_handles.get(id(subscription))

    async def read_values(self, node_ids: list[str]) -> list[float]:
        self.read_values_calls += 1
        return [float(i + self.read_values_calls) for i, _ in enumerate(node_ids)]

    async def read_data_values(self, node_ids: list[str]) -> list[DataValueDTO]:
        self.read_data_values_calls += 1
        results: list[DataValueDTO] = []
        for index, node_id in enumerate(node_ids):
            override = self.data_value_overrides.get(node_id)
            if override is not None:
                results.append(override)
                continue
            results.append(
                _signed_dto(
                    float(index + self.read_data_values_calls),
                    0x00000000,
                    datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                )
            )
        return results


@pytest.mark.parametrize("empty_first", [False, True])
def test_argument_dimensions_do_not_create_spurious_updates(empty_first: bool) -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1.0)
    state = _SubscriptionState(
        subscription_id="sub-argument",
        client_id="client",
        display_name=None,
        monitored_objects={},
    )
    null_argument = ua.Argument(Name="EventId", ValueRank=-1, ArrayDimensions=None)  # type: ignore[arg-type]
    empty_argument = ua.Argument(Name="EventId", ValueRank=-1, ArrayDimensions=[])
    first, second = (empty_argument, null_argument) if empty_first else (null_argument, empty_argument)

    service._append_update(state, "ns=2;s=InputArguments", normalize_opcua_value([first]))
    service._append_update(state, "ns=2;s=InputArguments", normalize_opcua_value([second]))

    assert state.sequence_number == 1
    assert len(state.updates) == 1
    assert state.updates[0].value[0].ArrayDimensions is None
    assert empty_argument.ArrayDimensions == []

    changed = ua.Argument(Name="Comment", ValueRank=-1, ArrayDimensions=[])
    service._append_update(state, "ns=2;s=InputArguments", normalize_opcua_value([changed]))
    assert state.sequence_number == 2


@pytest.mark.asyncio
async def test_negative_ack_beyond_minus_one_discards_all_updates() -> None:
    """Any negative ack is the documented `-1` "drop everything" sentinel (L9)."""
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1)
    created = await service.create_subscription(client_id="c1", display_name=None)
    subscription_id = created.subscription_id

    async with service._lock:
        state = service._subscriptions[subscription_id]
        service._append_update(state, "ns=2;s=Temperature", 1.0)
    assert state.updates

    synced = await service.sync("c1", subscription_id, acknowledge_sequence=-5)
    assert synced is not None
    assert synced.updates == []
    assert not state.updates
    await service.close()


@pytest.mark.asyncio
async def test_stream_active_sync_reports_overflow_without_consuming() -> None:
    """Polling while a stream is open must reveal dropped updates (L9)."""
    service = SubscriptionService(
        cast(OpcUaClientProtocol, FakeOpcUaClient()),
        interval_seconds=1,
        max_updates_per_subscription=2,
    )
    created = await service.create_subscription(client_id="c1", display_name=None)
    subscription_id = created.subscription_id

    async with service._lock:
        state = service._subscriptions[subscription_id]
        # Four updates against a queue of two drops sequences 1 and 2.
        service._append_update(state, "ns=2;s=Temperature", 1.0)
        service._append_update(state, "ns=2;s=Pressure", 2.0)
        service._append_update(state, "ns=2;s=Temperature", 3.0)
        service._append_update(state, "ns=2;s=Speed", 4.0)
    assert state.dropped_from_sequence == 1
    assert state.dropped_to_sequence == 2

    generation = await service.activate_stream("c1", subscription_id)
    assert generation is not None
    synced = await service.sync("c1", subscription_id)
    assert synced is not None
    assert synced.stream_active is True
    assert synced.queue_overflow is True
    assert synced.dropped_from_sequence == 1
    assert synced.dropped_to_sequence == 2

    # The markers survive the stream-active rejection and are consumed by the
    # first sync without an open stream.
    await service.deactivate_stream(subscription_id, generation)
    synced_after = await service.sync("c1", subscription_id)
    assert synced_after is not None
    assert synced_after.queue_overflow is True
    assert synced_after.dropped_from_sequence == 1
    assert synced_after.dropped_to_sequence == 2
    third_sync = await service.sync("c1", subscription_id)
    assert third_sync is not None
    assert third_sync.queue_overflow is False
    await service.close()


def _model() -> BuildResult:
    root = ModelNode(
        id="asset-root",
        name="Root",
        kind="asset",
        type=None,
        children=["prop-a", "asset-child"],
        source_node_id="ns=2;s=Root",
    )
    child = ModelNode(
        id="asset-child",
        name="Child",
        kind="asset",
        type=None,
        children=["prop-b"],
        source_node_id="ns=2;s=Child",
    )
    prop_a = ModelNode(
        id="prop-a",
        name="Temperature",
        kind="property",
        type="Double",
        children=[],
        source_node_id="ns=2;s=Temperature",
    )
    prop_b = ModelNode(
        id="prop-b",
        name="Pressure",
        kind="property",
        type="Double",
        children=[],
        source_node_id="ns=2;s=Pressure",
    )
    return BuildResult(
        nodes_by_id={root.id: root, child.id: child, prop_a.id: prop_a, prop_b.id: prop_b},
        root_ids=[root.id],
        children_by_id={root.id: [prop_a.id, child.id], child.id: [prop_b.id], prop_a.id: [], prop_b.id: []},
        # Relationship planes per i3X spec: prop-a is a composition child of the root,
        # asset-child is a hierarchical child (independent, never collected via the parent).
        hierarchy_children_by_id={root.id: [child.id]},
        composition_children_by_id={root.id: [prop_a.id], child.id: [prop_b.id]},
        instances_by_type_id={},
        property_to_node={prop_a.id: prop_a.source_node_id, prop_b.id: prop_b.source_node_id},
        action_to_method={},
    )


def _wide_model(width: int) -> BuildResult:
    """Asset whose composition plane holds ``width`` properties (for budget tests)."""
    props = [
        ModelNode(
            id=f"prop-{index}",
            name=f"Property {index}",
            kind="property",
            type="Double",
            children=[],
            source_node_id=f"ns=2;s=Property{index}",
        )
        for index in range(width)
    ]
    root = ModelNode(
        id="asset-root",
        name="Root",
        kind="asset",
        type=None,
        children=[prop.id for prop in props],
        source_node_id="ns=2;s=Root",
    )
    return BuildResult(
        nodes_by_id={root.id: root, **{prop.id: prop for prop in props}},
        root_ids=[root.id],
        children_by_id={root.id: [prop.id for prop in props], **{prop.id: [] for prop in props}},
        composition_children_by_id={root.id: [prop.id for prop in props]},
        instances_by_type_id={},
        property_to_node={prop.id: prop.source_node_id for prop in props},
        action_to_method={},
    )


@pytest.mark.asyncio
async def test_subscription_lifecycle_sync_wait_and_delete() -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1)
    created = await service.create_subscription(client_id="c1", display_name="s1")
    subscription_id = created.subscription_id

    model = _model()
    assert await service.register_items("c1", subscription_id, ["asset-root"], max_depth=0, model=model) is True

    await service.handle_datachange(subscription_id, "ns=2;s=Temperature", 12.3)
    synced = await service.sync("c1", subscription_id, acknowledge_sequence=0)
    assert synced is not None
    assert len(synced.updates) >= 1

    waited = await service.wait_for_updates("c1", subscription_id, after_sequence=0, timeout_seconds=1)
    assert waited is not None
    assert len(waited) >= 1

    deleted = await service.delete_subscriptions("c1", [subscription_id])
    assert deleted[0].success is True
    await service.close()


@pytest.mark.asyncio
async def test_register_seeds_initial_sync_updates() -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1)
    created = await service.create_subscription(client_id="c1", display_name="s1")
    subscription_id = created.subscription_id

    model = _model()
    assert await service.register_items("c1", subscription_id, ["asset-root"], max_depth=1, model=model) is True

    synced = await service.sync("c1", subscription_id, acknowledge_sequence=0)
    assert synced is not None
    assert [item.element_id for item in synced.updates] == ["prop-a"]
    await service.close()


@pytest.mark.asyncio
async def test_sync_acknowledge_sequence_removes_returned_updates() -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1)
    created = await service.create_subscription(client_id="c1", display_name="s1")
    subscription_id = created.subscription_id

    model = _model()
    assert (
        await service.register_items("c1", subscription_id, ["asset-root", "asset-child"], max_depth=2, model=model)
        is True
    )

    first = await service.sync("c1", subscription_id, acknowledge_sequence=0)
    assert first is not None
    assert [item.sequence_number for item in first.updates] == [1, 2]

    second = await service.sync("c1", subscription_id, acknowledge_sequence=2)
    assert second is not None
    assert second.updates == []
    await service.close()


@pytest.mark.asyncio
async def test_register_and_unregister_update_mappings_incrementally(monkeypatch: pytest.MonkeyPatch) -> None:
    """Small registrations must not rebuild the source index or re-resolve mapped objects."""
    builds = 0
    resolves: list[str] = []
    real_build = mapping._build_source_index
    real_resolve = mapping._resolve_object_mappings

    def counted_build(model: BuildResult) -> dict[str, ModelNode]:
        nonlocal builds
        builds += 1
        return real_build(model)

    def counted_resolve(
        element_id: str,
        max_depth: int,
        model: BuildResult,
        source_index: dict[str, ModelNode],
        max_nodes: int | None = None,
    ) -> dict[str, str]:
        resolves.append(element_id)
        return real_resolve(element_id, max_depth, model, source_index, max_nodes=max_nodes)

    monkeypatch.setattr(mapping, "_build_source_index", counted_build)
    monkeypatch.setattr(mapping, "_resolve_object_mappings", counted_resolve)

    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1)
    created = await service.create_subscription(client_id="c1", display_name=None)
    subscription_id = created.subscription_id
    model = _model()

    assert await service.register_items("c1", subscription_id, ["asset-root"], max_depth=2, model=model) is True
    assert await service.register_items("c1", subscription_id, ["asset-child"], max_depth=2, model=model) is True
    # Re-registering the same object at the same depth must reuse the stored mapping.
    assert await service.register_items("c1", subscription_id, ["asset-root"], max_depth=2, model=model) is True
    assert builds == 1
    assert resolves == ["asset-root", "asset-child"]

    sub_state = service._subscriptions[subscription_id]
    assert sub_state.monitored_node_ids == {"ns=2;s=Temperature", "ns=2;s=Pressure"}
    assert sub_state.node_to_element_id["ns=2;s=temperature"] == "prop-a"

    assert await service.unregister_items("c1", subscription_id, ["asset-child"], model=model) is True
    assert builds == 1
    assert resolves == ["asset-root", "asset-child"]
    assert sub_state.monitored_node_ids == {"ns=2;s=Temperature"}
    assert "ns=2;s=Pressure" not in sub_state.node_to_element_id
    assert "ns=2;s=pressure" not in sub_state.node_to_element_id

    # A changed model revision rebuilds every mapping against the new model.
    refreshed = _model()
    refreshed.nodes_by_id["prop-b"].source_node_id = "ns=3;s=Pressure"
    assert await service.register_items("c1", subscription_id, ["asset-child"], max_depth=2, model=refreshed) is True
    assert builds == 2
    assert resolves == ["asset-root", "asset-child", "asset-root", "asset-child"]
    assert sub_state.monitored_node_ids == {"ns=2;s=Temperature", "ns=3;s=Pressure"}
    assert sub_state.node_to_element_id["ns=3;s=pressure"] == "prop-b"
    await service.close()


@pytest.mark.asyncio
async def test_sync_refreshes_changed_values_when_queue_empty_and_no_ack() -> None:
    client = FakeOpcUaClient()
    service = SubscriptionService(
        cast(OpcUaClientProtocol, client),
        interval_seconds=0.2,
        seed_initial_values=True,
    )
    created = await service.create_subscription(client_id="c1", display_name="s1")
    subscription_id = created.subscription_id

    model = _model()
    assert await service.register_items("c1", subscription_id, ["asset-root"], max_depth=1, model=model) is True

    first = await service.sync("c1", subscription_id, acknowledge_sequence=None)
    assert first is not None
    assert first.updates
    last = first.updates[-1].sequence_number

    second = await service.sync("c1", subscription_id, acknowledge_sequence=last)
    assert second is not None
    assert second.updates == []

    third = await service.sync("c1", subscription_id, acknowledge_sequence=None)
    assert third is not None
    assert third.updates
    assert all(item.sequence_number > last for item in third.updates)
    assert client.read_data_values_calls >= 2
    await service.close()


@pytest.mark.asyncio
async def test_initial_snapshot_is_not_suppressed_across_subscriptions() -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1)
    model = _model()

    first_created = await service.create_subscription(client_id="c1", display_name="s1")
    second_created = await service.create_subscription(client_id="c2", display_name="s2")

    assert (
        await service.register_items(
            "c1",
            first_created.subscription_id,
            ["asset-root"],
            max_depth=1,
            model=model,
        )
        is True
    )
    assert (
        await service.register_items(
            "c2",
            second_created.subscription_id,
            ["asset-root"],
            max_depth=1,
            model=model,
        )
        is True
    )

    first_sync = await service.sync("c1", first_created.subscription_id, acknowledge_sequence=0)
    second_sync = await service.sync("c2", second_created.subscription_id, acknowledge_sequence=0)

    assert first_sync is not None
    assert second_sync is not None
    assert [item.element_id for item in first_sync.updates] == ["prop-a"]
    assert [item.element_id for item in second_sync.updates] == ["prop-a"]
    await service.close()


@pytest.mark.asyncio
async def test_register_does_not_seed_initial_sync_when_disabled() -> None:
    service = SubscriptionService(
        cast(OpcUaClientProtocol, FakeOpcUaClient()),
        interval_seconds=1,
        seed_initial_values=False,
    )
    created = await service.create_subscription(client_id="c1", display_name="s1")
    subscription_id = created.subscription_id

    model = _model()
    assert await service.register_items("c1", subscription_id, ["asset-root"], max_depth=1, model=model) is True

    synced = await service.sync("c1", subscription_id, acknowledge_sequence=0)
    assert synced is not None
    assert synced.updates == []
    await service.close()


@pytest.mark.asyncio
async def test_subscription_service_accepts_subsecond_interval() -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=0.25)
    assert service._interval_seconds == 0.25
    await service.close()


@pytest.mark.asyncio
async def test_unregister_unknown_subscription_returns_false() -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1)
    model = _model()
    assert await service.unregister_items("c1", "missing", ["asset-root"], model=model) is False
    await service.close()


class _SingleHandleOpcUaClient(FakeOpcUaClient):
    async def subscribe_data_changes(self, subscription: Any, node_ids: list[str]) -> int:
        del subscription, node_ids
        return 11


@pytest.mark.asyncio
async def test_single_monitored_node_remaps_unmapped_node_id() -> None:
    """A single monitored node accepts notifications with diverging node-id formats."""
    service = SubscriptionService(cast(OpcUaClientProtocol, _SingleHandleOpcUaClient()), interval_seconds=1)
    created = await service.create_subscription(client_id="c1", display_name=None)
    subscription_id = created.subscription_id

    model = _model()
    assert await service.register_items("c1", subscription_id, ["prop-a"], max_depth=1, model=model) is True

    await service.handle_datachange(subscription_id, "ns=2;s=Unmapped", 44.2)
    synced = await service.sync("c1", subscription_id, acknowledge_sequence=0)
    assert synced is not None
    assert synced.updates
    assert synced.updates[-1].element_id == "prop-a"
    await service.close()


@pytest.mark.asyncio
async def test_single_monitored_object_fallbacks_element_id_when_mapping_missing() -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1)
    created = await service.create_subscription(client_id="c1", display_name=None)
    subscription_id = created.subscription_id

    model = _model()
    assert await service.register_items("c1", subscription_id, ["prop-a"], max_depth=1, model=model) is True

    async with service._lock:
        state = service._subscriptions[subscription_id]
        state.node_to_element_id.clear()

    await service.handle_datachange(subscription_id, "nsu=http://example/;s=Temperature", 60.1)
    synced = await service.sync("c1", subscription_id, acknowledge_sequence=0)
    assert synced is not None
    assert synced.updates
    assert synced.updates[-1].element_id == "prop-a"
    await service.close()


@pytest.mark.asyncio
async def test_shared_polling_loop_collects_updates() -> None:
    client = FakeOpcUaClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=1)
    created = await service.create_subscription(client_id="c1", display_name="poll")
    subscription_id = created.subscription_id

    model = _model()
    await service.register_items("c1", subscription_id, ["asset-root"], max_depth=2, model=model)

    node_id = "ns=2;s=Temperature"
    async with service._lock:
        monitor = _NodeMonitor(node_id=node_id, mode="polling")
        monitor.subscriber_ids.add(subscription_id)
        service._node_monitors[node_id] = monitor

    task = asyncio.create_task(service._shared_polling_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    await task

    synced = await service.sync("c1", subscription_id, acknowledge_sequence=0)
    assert synced is not None
    assert len(synced.updates) >= 1
    await service.close()


@pytest.mark.asyncio
async def test_shared_queue_overflow_marks_dropped_sequence_range() -> None:
    """Updates dropped on the shared datachange queue surface as a sequence gap, not silently."""
    service = SubscriptionService(
        cast(OpcUaClientProtocol, FakeOpcUaClient()),
        interval_seconds=1,
        max_updates_per_subscription=10,
        seed_initial_values=False,
    )
    # Shrink the shared queue so enqueueing three values drops the oldest two.
    service._node_datachange_queue = asyncio.Queue(maxsize=1)
    created = await service.create_subscription(client_id="c1", display_name=None)
    subscription_id = created.subscription_id

    model = _model()
    assert await service.register_items("c1", subscription_id, ["prop-a"], max_depth=1, model=model) is True

    service.schedule_node_datachange("ns=2;s=Temperature", 1.0)
    service.schedule_node_datachange("ns=2;s=Temperature", 2.0)
    service.schedule_node_datachange("ns=2;s=Temperature", 3.0)

    # Drain the surviving entry and fan it out directly so the test stays deterministic.
    node_id, value, signal = await service._node_datachange_queue.get()
    await service._fanout_node_value(node_id, value, signal)

    synced = await service.sync("c1", subscription_id, acknowledge_sequence=0)
    assert synced is not None
    assert synced.queue_overflow is True
    assert synced.dropped_from_sequence == 1
    assert synced.dropped_to_sequence == 2
    assert [item.sequence_number for item in synced.updates] == [3]
    assert synced.updates[0].value == 3.0
    await service.close()


class _SlowReadOpcUaClient(FakeOpcUaClient):
    """Reads that take time must not push the whole polling schedule backwards."""

    async def read_data_values(self, node_ids: list[str]) -> list[DataValueDTO]:
        self.read_data_values_calls += 1
        await asyncio.sleep(0.03)
        return await super().read_data_values(node_ids)


@pytest.mark.asyncio
async def test_shared_polling_loop_uses_absolute_deadline_schedule() -> None:
    """Ticks are scheduled against a deadline: a slow read shortens the next sleep instead of drifting."""
    client = _SlowReadOpcUaClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=0.05)
    created = await service.create_subscription(client_id="c1", display_name="poll")
    subscription_id = created.subscription_id

    model = _model()
    await service.register_items("c1", subscription_id, ["asset-root"], max_depth=2, model=model)

    node_id = "ns=2;s=Temperature"
    async with service._lock:
        monitor = _NodeMonitor(node_id=node_id, mode="polling")
        monitor.subscriber_ids.add(subscription_id)
        service._node_monitors[node_id] = monitor

    task = asyncio.create_task(service._shared_polling_loop())
    started = time.monotonic()
    while client.read_data_values_calls < 6:
        if time.monotonic() - started > 1.5:
            pytest.fail("shared polling loop did not produce 6 ticks within 1.5s")
        await asyncio.sleep(0.01)
    elapsed = time.monotonic() - started
    task.cancel()
    await task

    # Absolute-deadline ticks fire every 0.05s regardless of read duration, so six ticks
    # land around 0.25s; sleep-after-read drift needs ~0.40s. The bound discriminates the
    # drift while tolerating scheduler jitter on loaded runners.
    assert elapsed < 0.42
    assert client.read_data_values_calls >= 6
    await service.close()


@pytest.mark.asyncio
async def test_shared_polling_loop_fans_out_all_nodes_per_tick() -> None:
    """One tick batches the read and fans out every monitored node concurrently."""
    client = FakeOpcUaClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=0.05)
    created = await service.create_subscription(client_id="c1", display_name="poll")
    subscription_id = created.subscription_id

    model = _model()
    await service.register_items("c1", subscription_id, ["prop-a", "prop-b"], max_depth=1, model=model)

    async with service._lock:
        for node_id in ("ns=2;s=Temperature", "ns=2;s=Pressure"):
            monitor = _NodeMonitor(node_id=node_id, mode="polling")
            monitor.subscriber_ids.add(subscription_id)
            service._node_monitors[node_id] = monitor

    task = asyncio.create_task(service._shared_polling_loop())
    await asyncio.sleep(0.12)
    task.cancel()
    await task

    assert client.read_data_values_calls >= 1
    synced = await service.sync("c1", subscription_id, acknowledge_sequence=0)
    assert synced is not None
    assert {item.element_id for item in synced.updates} == {"prop-a", "prop-b"}
    await service.close()


@pytest.mark.asyncio
async def test_wait_for_updates_refreshes_native_timeout_without_callbacks() -> None:
    client = FakeOpcUaClient()
    service = SubscriptionService(
        cast(OpcUaClientProtocol, client),
        interval_seconds=1,
        native_timeout_refresh_mode="hybrid",
    )
    created = await service.create_subscription(client_id="c1", display_name="native")
    subscription_id = created.subscription_id

    model = _model()
    await service.register_items("c1", subscription_id, ["asset-root"], max_depth=1, model=model)

    async with service._lock:
        state = service._subscriptions[subscription_id]
        # Simulate native mode with a stale queue and no callback notifications.
        state.mode = "native"
        state.updates.clear()

    updates = await service.wait_for_updates("c1", subscription_id, after_sequence=0, timeout_seconds=0)
    assert updates is not None
    assert updates
    await service.close()


@pytest.mark.asyncio
async def test_can_open_new_bin_limits_and_helpers() -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1)

    async with service._lock:
        service._bins["bin-existing"] = _ManagedBin(
            bin_id="bin-existing",
            ua_subscription=object(),
            capacity=2,
            node_handles={"ns=2;s=Existing": 1},
        )

    caps = OpcUaSubscriptionCapabilities(
        max_monitored_items_per_call=1,
        max_subscriptions=2,
        max_monitored_items=3,
        max_subscriptions_per_session=2,
        max_monitored_items_per_subscription=2,
    )
    # The check is synchronous: it runs inside ``self._lock`` while an admission selects a bin.
    assert service._can_open_new_bin(caps) is True
    assert (
        service._can_open_new_bin(
            OpcUaSubscriptionCapabilities(
                max_monitored_items_per_call=1,
                max_subscriptions=1,
                max_monitored_items=3,
                max_subscriptions_per_session=2,
                max_monitored_items_per_subscription=2,
            )
        )
        is False
    )
    # An admission whose round-trip is still in flight already consumes the monitored-item budget.
    service._bins["bin-existing"].pending_nodes = ["ns=2;s=pending-a", "ns=2;s=pending-b"]
    assert service._can_open_new_bin(caps) is False

    assert _min_positive(None, 0, -1) is None
    assert _min_positive(None, 5, 2) == 2
    await service.close()


@pytest.mark.asyncio
async def test_subscription_count_cap_rejects_and_frees_slots() -> None:
    """``I3X_SUBSCRIPTION_MAX_COUNT`` caps *live* subscriptions; a rejection must create no state."""
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1, max_subscriptions=2)
    first = await service.create_subscription(client_id="c1")
    second = await service.create_subscription(client_id="c2")

    with pytest.raises(AdmissionLimitError) as exc_info:
        await service.create_subscription(client_id="c3")
    assert exc_info.value.status_code == 429
    assert exc_info.value.error_code == "SubscriptionLimit"
    assert exc_info.value.details == {"maxSubscriptions": 2, "activeSubscriptions": 2}
    assert set(service._subscriptions) == {first.subscription_id, second.subscription_id}

    # Deleting frees a slot, so the cap is not a lifetime quota.
    await service.delete_subscriptions(None, [second.subscription_id])
    third = await service.create_subscription(client_id="c3")
    assert third.subscription_id not in {first.subscription_id, second.subscription_id}
    assert len(service._subscriptions) == 2
    await service.close()


@pytest.mark.asyncio
async def test_register_items_rejects_subtree_budget_without_truncating() -> None:
    """An oversized subtree is rejected with the error envelope; the monitored set stays intact."""
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1, subtree_max_nodes=2)
    created = await service.create_subscription(client_id="c1")
    subscription_id = created.subscription_id
    model = _wide_model(3)

    assert await service.register_items("c1", subscription_id, ["prop-0"], max_depth=1, model=model) is True
    state_obj = service._subscriptions[subscription_id]
    assert state_obj.monitored_node_ids == {"ns=2;s=Property0"}

    # The root collects all three properties: 3 monitored nodes exceed the budget of 2.
    with pytest.raises(AdmissionLimitError) as exc_info:
        await service.register_items("c1", subscription_id, ["asset-root"], max_depth=0, model=model)
    assert exc_info.value.status_code == 400
    assert exc_info.value.error_code == "MonitoredItemBudget"
    assert exc_info.value.details["maxNodes"] == 2
    assert exc_info.value.details["monitoredNodes"] == 3
    # Rejected registrations are atomic: the previous mapping is neither extended nor lost.
    assert state_obj.monitored_node_ids == {"ns=2;s=Property0"}
    assert list(state_obj.object_mappings) == ["prop-0"]
    assert state_obj.monitored_objects == {"prop-0": 1}
    await service.close()


@pytest.mark.asyncio
async def test_register_union_of_subtrees_exceeds_budget_only_across_objects() -> None:
    """Each subtree fits the cap on its own; the *union* across registrations is what the cap bounds."""
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1, subtree_max_nodes=1)
    created = await service.create_subscription(client_id="c1")
    subscription_id = created.subscription_id
    model = _model()

    # ``asset-child`` contributes Pressure; ``asset-root`` contributes Temperature - one node each,
    # so neither registration trips the per-object bound, but together they exceed it.
    assert await service.register_items("c1", subscription_id, ["asset-child"], max_depth=0, model=model) is True
    state_obj = service._subscriptions[subscription_id]
    assert state_obj.monitored_node_ids == {"ns=2;s=Pressure"}

    with pytest.raises(AdmissionLimitError) as exc_info:
        await service.register_items("c1", subscription_id, ["asset-root"], max_depth=0, model=model)
    assert exc_info.value.status_code == 400
    assert exc_info.value.details == {"subscriptionId": subscription_id, "monitoredNodes": 2, "maxNodes": 1}
    assert state_obj.monitored_node_ids == {"ns=2;s=Pressure"}
    assert state_obj.monitored_objects == {"asset-child": 0}
    await service.close()


def test_mapping_budget_names_the_offending_object_and_counts_unions() -> None:
    model = _wide_model(3)

    with pytest.raises(AdmissionLimitError) as exc_info:
        mapping._collect_property_source_mappings(model, model.nodes_by_id["asset-root"], max_depth=0, max_nodes=2)
    assert exc_info.value.details == {"elementId": "asset-root", "monitoredNodes": 3, "maxNodes": 2}
    # Overlapping subtrees share source nodes, so the budget must count the union, not the sum.
    node_ids, _lookup = mapping._resolve_monitored_node_ids({"prop-0": 1, "asset-root": 0}, model, max_nodes=3)
    assert node_ids == {"ns=2;s=Property0", "ns=2;s=Property1", "ns=2;s=Property2"}

    with pytest.raises(AdmissionLimitError):
        mapping._resolve_monitored_node_ids({"prop-0": 1, "asset-root": 0}, model, max_nodes=2)


@pytest.mark.asyncio
async def test_native_admission_falls_back_to_process_local_caps() -> None:
    """A server advertising no capability variables must not allow unbounded bins/items."""
    service = SubscriptionService(
        cast(OpcUaClientProtocol, FakeOpcUaClient()),
        interval_seconds=1,
        native_max_bins=1,
        native_max_monitored_items=2,
    )
    no_caps = OpcUaSubscriptionCapabilities(
        max_monitored_items_per_call=None,
        max_subscriptions=None,
        max_monitored_items=None,
        max_subscriptions_per_session=None,
        max_monitored_items_per_subscription=None,
    )

    async with service._lock:
        assert service._can_open_new_bin(no_caps) is True
        service._bins["bin-1"] = _ManagedBin(bin_id="bin-1", ua_subscription=object(), capacity=2)
        # The bin floor (1) is reached even though the server reported no subscription limit.
        assert service._can_open_new_bin(no_caps) is False
        service._bins["bin-1"].node_handles = {"ns=2;s=A": 1, "ns=2;s=B": 2}
        # The monitored-item floor (2) is reached too.
        assert service._can_open_new_bin(no_caps) is False

    # A generous server limit is capped by the local floor; a tighter one stays effective.
    async with service._lock:
        service._bins.clear()
        generous = OpcUaSubscriptionCapabilities(
            max_monitored_items_per_call=None,
            max_subscriptions=1000,
            max_monitored_items=1000,
            max_subscriptions_per_session=1000,
            max_monitored_items_per_subscription=None,
        )
        assert service._can_open_new_bin(generous) is True
        service._bins["bin-2"] = _ManagedBin(bin_id="bin-2", ua_subscription=object(), capacity=2)
        assert service._can_open_new_bin(generous) is False

        tighter = OpcUaSubscriptionCapabilities(
            max_monitored_items_per_call=None,
            max_subscriptions=100,
            max_monitored_items=1,
            max_subscriptions_per_session=100,
            max_monitored_items_per_subscription=None,
        )
        service._bins.clear()
        assert service._can_open_new_bin(tighter) is True
        service._bins["bin-3"] = _ManagedBin(bin_id="bin-3", ua_subscription=object(), capacity=2)
        assert service._can_open_new_bin(tighter) is False

    assert _capped_limit(None, 4) == 4
    assert _capped_limit(0, 4) == 4
    assert _capped_limit(2, 4) == 2
    assert _capped_limit(100, 4) == 4
    await service.close()


@pytest.mark.asyncio
async def test_native_admissions_run_network_calls_concurrently() -> None:
    """Admission selection must not hold its lock across the OPC UA round-trip (M9).

    ``FakeOpcUaClient`` only lets monitored-item adds return once both nodes have arrived in
    total, so an admission that serialized its network work would deadlock: the first call
    would never finish and never let the second registration reach selection. Bin packing must
    still hold - one create for both nodes, and each node ends up with exactly one item.
    """

    class ConcurrentAddClient(FakeOpcUaClient):
        def __init__(self) -> None:
            super().__init__()
            self.add_arrivals = 0
            self.arrived_batches: list[list[str]] = []
            self.all_arrived = asyncio.Event()

        async def subscribe_data_changes(self, subscription: Any, node_ids: list[str]) -> list[int]:
            del subscription
            self.add_arrivals += 1
            self.arrived_batches.append(list(node_ids))
            # Releases on either shape: one batch covering both nodes, or two single-node adds.
            if sum(len(batch) for batch in self.arrived_batches) >= 2:
                self.all_arrived.set()
            await self.all_arrived.wait()
            handles: list[int] = []
            for _ in node_ids:
                self.subscribe_calls += 1
                handles.append(self.subscribe_calls)
            return handles

    client = ConcurrentAddClient()
    service = SubscriptionService(
        cast(OpcUaClientProtocol, client),
        interval_seconds=1,
        seed_initial_values=False,
        max_concurrent_native_admissions=2,
    )
    model = _model()
    sub_a = await service.create_subscription(client_id="client-a", display_name="parallel-a")
    sub_b = await service.create_subscription(client_id="client-b", display_name="parallel-b")

    registered = await asyncio.wait_for(
        asyncio.gather(
            service.register_items("client-a", sub_a.subscription_id, ["prop-a"], max_depth=0, model=model),
            service.register_items("client-b", sub_b.subscription_id, ["prop-b"], max_depth=0, model=model),
        ),
        timeout=5,
    )
    # ``gather`` returns a list at runtime; the typeshed stub declares a tuple.
    assert list(registered) == [True, True]
    # One create for both nodes; every reserved node is subscribed at least once and ends up
    # with exactly one committed handle (a late reservation may add itself separately, and the
    # duplicate round-trip is released). The deterministic batch case is a separate test.
    assert client.create_subscription_calls == 1
    arrived_nodes = {node for batch in client.arrived_batches for node in batch}
    assert arrived_nodes == {"ns=2;s=Temperature", "ns=2;s=Pressure"}

    async with service._lock:
        # Both nodes share one bin, and the in-flight reservations were committed after the batch.
        assert len(service._bins) == 1
        bin_ = next(iter(service._bins.values()))
        assert set(bin_.node_handles.keys()) == {"ns=2;s=Temperature", "ns=2;s=Pressure"}
        assert bin_.pending_nodes == []

    await service.close()


@pytest.mark.asyncio
async def test_open_new_bin_batches_all_reserved_nodes_into_one_add() -> None:
    """N reservations against a fresh bin cost one CreateMonitoredItems round-trip, not N."""

    class BatchRecordingClient(FakeOpcUaClient):
        def __init__(self) -> None:
            super().__init__()
            self.batches: list[list[str]] = []

        async def subscribe_data_changes(self, subscription: Any, node_ids: list[str]) -> list[int]:
            self.batches.append(list(node_ids))
            return await super().subscribe_data_changes(subscription, node_ids)

    client = BatchRecordingClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=1, seed_initial_values=False)

    node_ids = ["ns=2;s=A", "ns=2;s=B", "ns=2;s=C"]
    monitors = [_NodeMonitor(node_id=node_id) for node_id in node_ids]
    placeholder = _ManagedBin(bin_id="bin-batch", ua_subscription=None, capacity=10)
    async with service._lock:
        for monitor in monitors:
            service._node_monitors[monitor.node_id] = monitor
        service._bins[placeholder.bin_id] = placeholder
        # The state ``_acquire_native_bin_slot`` leaves behind: three reservations, no subscription.
        placeholder.pending_nodes = list(node_ids)

    assert await _open_new_bin(service, monitors[0], placeholder) is True

    assert client.batches == [node_ids]
    assert client.create_subscription_calls == 1
    async with service._lock:
        assert len(placeholder.node_handles) == 3
        assert placeholder.pending_nodes == []
        assert placeholder.ready.is_set()
        assert all(monitor.mode == "native" and monitor.bin_id == placeholder.bin_id for monitor in monitors)
    await service.close()


@pytest.mark.asyncio
async def test_open_new_bin_excludes_reservation_of_released_monitor() -> None:
    """A monitor released while its reservation was pending must not be subscribed or leak budget."""

    class BatchRecordingClient(FakeOpcUaClient):
        def __init__(self) -> None:
            super().__init__()
            self.batches: list[list[str]] = []

        async def subscribe_data_changes(self, subscription: Any, node_ids: list[str]) -> list[int]:
            self.batches.append(list(node_ids))
            return await super().subscribe_data_changes(subscription, node_ids)

    client = BatchRecordingClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=1, seed_initial_values=False)

    keep = _NodeMonitor(node_id="ns=2;s=A")
    gone = _NodeMonitor(node_id="ns=2;s=B")
    placeholder = _ManagedBin(bin_id="bin-release", ua_subscription=None, capacity=10)
    async with service._lock:
        service._node_monitors["ns=2;s=A"] = keep
        service._node_monitors["ns=2;s=B"] = gone
        service._bins[placeholder.bin_id] = placeholder
        placeholder.pending_nodes = ["ns=2;s=A", "ns=2;s=B"]
        # The monitor for B disappears before the batch snapshot runs.
        del service._node_monitors["ns=2;s=B"]
        del gone

    assert await _open_new_bin(service, keep, placeholder) is True
    assert client.batches == [["ns=2;s=A"]]
    async with service._lock:
        assert set(placeholder.node_handles) == {"ns=2;s=A"}
        # The vanished reservation must stop counting towards the monitored-item budget.
        assert placeholder.pending_nodes == []
    await service.close()


@pytest.mark.asyncio
async def test_open_new_bin_after_concurrent_reclaim_deletes_subscription() -> None:
    """A bin reclaimed while its subscription was created must not keep publishing."""
    client = FakeOpcUaClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=1, seed_initial_values=False)
    monitor = _NodeMonitor(node_id="ns=2;s=A")
    placeholder = _ManagedBin(bin_id="bin-reclaim", ua_subscription=None, capacity=10)
    async with service._lock:
        service._node_monitors[monitor.node_id] = monitor
        service._bins[placeholder.bin_id] = placeholder
        placeholder.pending_nodes = ["ns=2;s=A"]
        # The last subscriber vanished while the round-trip was in flight.
        del service._bins[placeholder.bin_id]

    assert await _open_new_bin(service, monitor, placeholder) is False
    assert len(client.deleted_subscriptions) == 1
    assert monitor.mode == "idle"
    await service.close()


@pytest.mark.asyncio
async def test_open_new_bin_releases_handles_of_monitors_released_mid_batch() -> None:
    """Handles for monitors released during the batch must be unsubscribed and the bin reclaimed."""

    class ReleaseDuringSubscribeClient(FakeOpcUaClient):
        def __init__(self, service: SubscriptionService) -> None:
            super().__init__()
            self._service = service

        async def subscribe_data_changes(self, subscription: Any, node_ids: list[str]) -> list[int]:
            # The only subscriber vanishes while the batch round-trip is in flight.
            async with self._service._lock:
                del self._service._node_monitors[node_ids[0]]
            return await super().subscribe_data_changes(subscription, node_ids)

    service = SubscriptionService(
        cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1, seed_initial_values=False
    )
    # The fake needs the registry lock target, so it is wired in after the service exists.
    client = ReleaseDuringSubscribeClient(service)
    service._opcua_client = cast(OpcUaClientProtocol, client)

    monitor = _NodeMonitor(node_id="ns=2;s=A")
    placeholder = _ManagedBin(bin_id="bin-mid-release", ua_subscription=None, capacity=10)
    async with service._lock:
        service._node_monitors[monitor.node_id] = monitor
        service._bins[placeholder.bin_id] = placeholder
        placeholder.pending_nodes = ["ns=2;s=A"]

    assert await _open_new_bin(service, monitor, placeholder) is False
    assert client.unsubscribed_calls, "orphaned batch handle must be unsubscribed"
    assert client.deleted_subscriptions, "empty bin must delete its subscription"
    async with service._lock:
        assert placeholder.bin_id not in service._bins
    await service.close()


@pytest.mark.asyncio
async def test_add_node_to_bin_adopts_committed_batch_handle_without_round_trip() -> None:
    """A node already subscribed by the creating batch needs no second CreateMonitoredItems."""
    client = FakeOpcUaClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=1, seed_initial_values=False)
    bin_ = _ManagedBin(
        bin_id="bin-adopt",
        ua_subscription=SimpleNamespace(id="ua-adopt"),
        capacity=10,
        node_handles={"ns=2;s=A": 5},
    )
    bin_.ready.set()
    monitor = _NodeMonitor(node_id="ns=2;s=A", mode="native", bin_id=bin_.bin_id)
    async with service._lock:
        service._node_monitors[monitor.node_id] = monitor
        service._bins[bin_.bin_id] = bin_

    assert await _add_node_to_bin(service, monitor, bin_) is True
    assert client.subscribe_calls == 0

    # A batched handle whose monitor vanished concurrently is released instead of adopted.
    async with service._lock:
        del service._node_monitors[monitor.node_id]
        bin_.node_handles["ns=2;s=B"] = 6
    orphan_monitor = _NodeMonitor(node_id="ns=2;s=B")  # not in the registry: released
    assert await _add_node_to_bin(service, orphan_monitor, bin_) is False
    assert client.unsubscribed_calls == [(bin_.ua_subscription, [6])]
    async with service._lock:
        assert "ns=2;s=B" not in bin_.node_handles
    await service.close()


@pytest.mark.asyncio
async def test_add_node_to_bin_releases_duplicate_of_batch_committed_handle() -> None:
    """If the batch commits while a single-node add races it, the duplicate handle is released."""

    class BatchRaceClient(FakeOpcUaClient):
        def __init__(self, service: SubscriptionService, bin_: _ManagedBin, monitor: _NodeMonitor) -> None:
            super().__init__()
            self._service = service
            self._bin = bin_
            self._monitor = monitor

        async def subscribe_data_changes(self, subscription: Any, node_ids: list[str]) -> list[int]:
            # The creating admission commits this node's batched handle mid-round-trip, exactly
            # as ``_open_new_bin`` does: handle committed and reservation released.
            async with self._service._lock:
                self._bin.node_handles[node_ids[0]] = 42
                if node_ids[0] in self._bin.pending_nodes:
                    self._bin.pending_nodes.remove(node_ids[0])
                self._monitor.mode = "native"
                self._monitor.bin_id = self._bin.bin_id
            return await super().subscribe_data_changes(subscription, node_ids)

    service = SubscriptionService(
        cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1, seed_initial_values=False
    )
    bin_ = _ManagedBin(bin_id="bin-race", ua_subscription=SimpleNamespace(id="ua-race"), capacity=10)
    bin_.ready.set()
    monitor = _NodeMonitor(node_id="ns=2;s=A")
    # The fake needs the bin and monitor under test, so it is wired in after they exist.
    client = BatchRaceClient(service, bin_, monitor)
    service._opcua_client = cast(OpcUaClientProtocol, client)
    async with service._lock:
        service._node_monitors[monitor.node_id] = monitor
        service._bins[bin_.bin_id] = bin_
        bin_.pending_nodes = ["ns=2;s=A"]

    assert await _add_node_to_bin(service, monitor, bin_) is True
    # The batched handle stays; the racing round-trip's handle is released.
    assert bin_.node_handles == {"ns=2;s=A": 42}
    assert client.unsubscribed_calls == [(bin_.ua_subscription, [1])]
    assert bin_.pending_nodes == []
    await service.close()


@pytest.mark.asyncio
async def test_add_node_to_bin_reclaims_placeholder_with_failed_create() -> None:
    """A placeholder whose create failed has no subscription: its last reservation must free it."""
    client = FakeOpcUaClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=1, seed_initial_values=False)
    placeholder = _ManagedBin(bin_id="bin-failed", ua_subscription=None, capacity=10)
    placeholder.ready.set()
    monitor = _NodeMonitor(node_id="ns=2;s=A")
    async with service._lock:
        service._node_monitors[monitor.node_id] = monitor
        service._bins[placeholder.bin_id] = placeholder
        placeholder.pending_nodes = ["ns=2;s=A"]

    assert await _add_node_to_bin(service, monitor, placeholder) is False
    async with service._lock:
        assert "bin-failed" not in service._bins
    await service.close()


@pytest.mark.asyncio
async def test_stale_monitored_item_cleanup_does_not_hold_the_lock() -> None:
    """A failed/stale admission cleanup must not block fan-out or sync for a full round-trip (M9)."""
    client = FakeOpcUaClient()
    gate = asyncio.Event()

    async def slow_unsubscribe_data_changes(subscription: Any, handles: list[int]) -> None:
        client.unsubscribed_calls.append((subscription, list(handles)))
        await gate.wait()

    client.unsubscribe_data_changes = slow_unsubscribe_data_changes  # type: ignore[method-assign]

    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=1, seed_initial_values=False)
    model = _model()
    created = await service.create_subscription(client_id="c1", display_name="stale")
    assert await service.register_items("c1", created.subscription_id, ["prop-a"], max_depth=0, model=model) is True
    target_bin = next(iter(service._bins.values()))

    # A monitor that no longer exists: the item has to be unsubscribed, but outside the lock.
    monitor = _NodeMonitor(node_id="ns=2;s=Pressure")
    add_task = asyncio.create_task(service._add_node_to_bin(monitor, target_bin))
    await asyncio.sleep(0.05)

    synced = await asyncio.wait_for(service.sync("c1", created.subscription_id, acknowledge_sequence=None), timeout=0.5)
    assert synced is not None

    gate.set()
    assert await add_task is False
    assert client.unsubscribed_calls
    await service.close()


@pytest.mark.asyncio
async def test_two_subscriptions_on_same_node_share_native_runtime() -> None:
    """Phase 1 of the shared-monitoring plan: two subscriptions on the same node
    share a single native OPC UA subscription and see the same datachange fan-out.
    """
    client = FakeOpcUaClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=1, seed_initial_values=False)
    model = _model()

    sub_a = await service.create_subscription(client_id="client-a", display_name="poll")
    sub_b = await service.create_subscription(client_id="client-b", display_name="stream")
    assert await service.register_items("client-a", sub_a.subscription_id, ["prop-a"], max_depth=0, model=model)
    assert await service.register_items("client-b", sub_b.subscription_id, ["prop-a"], max_depth=0, model=model)

    # Only one native OPC UA subscription was created for the shared node.
    assert client.create_subscription_calls == 1

    async with service._lock:
        monitor = service._node_monitors["ns=2;s=Temperature"]
        assert monitor.subscriber_ids == {sub_a.subscription_id, sub_b.subscription_id}

    # A single node-level datachange fans out to both subscriptions' buffers.
    await service._fanout_node_value("ns=2;s=Temperature", 42.0)
    synced_a = await service.sync("client-a", sub_a.subscription_id, acknowledge_sequence=0)
    synced_b = await service.sync("client-b", sub_b.subscription_id, acknowledge_sequence=0)
    assert synced_a is not None and synced_a.updates and synced_a.updates[0].value == 42.0
    assert synced_b is not None and synced_b.updates and synced_b.updates[0].value == 42.0

    # Releasing one subscription keeps the shared monitor alive for the other.
    assert await service.unregister_items("client-a", sub_a.subscription_id, ["prop-a"], model=model) is True
    async with service._lock:
        assert "ns=2;s=Temperature" in service._node_monitors
        assert service._node_monitors["ns=2;s=Temperature"].subscriber_ids == {sub_b.subscription_id}

    # Releasing the last subscriber reclaims/deletes the shared OPC UA subscription.
    assert await service.unregister_items("client-b", sub_b.subscription_id, ["prop-a"], model=model) is True
    async with service._lock:
        assert "ns=2;s=Temperature" not in service._node_monitors
    assert len(client.deleted_subscriptions) == 1

    await service.close()


@pytest.mark.asyncio
async def test_sync_acknowledge_minus_one_clears_queue() -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1)
    created = await service.create_subscription(client_id="c1", display_name="s1")
    subscription_id = created.subscription_id
    model = _model()
    assert await service.register_items("c1", subscription_id, ["asset-root"], max_depth=1, model=model) is True

    await service.handle_datachange(subscription_id, "ns=2;s=Temperature", 1.0)
    await service.handle_datachange(subscription_id, "ns=2;s=Pressure", 2.0)

    synced = await service.sync("c1", subscription_id, acknowledge_sequence=-1)
    assert synced is not None
    assert synced.updates == []
    await service.close()


@pytest.mark.asyncio
async def test_null_subscription_update_uses_goodnodata_quality() -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1)
    created = await service.create_subscription(client_id="c1", display_name="s1")
    subscription_id = created.subscription_id

    async with service._lock:
        state = service._subscriptions[subscription_id]
        state.node_to_element_id["ns=2;s=Temperature"] = "prop-a"

    await service.handle_datachange(subscription_id, "ns=2;s=Temperature", None)
    synced = await service.sync("c1", subscription_id, acknowledge_sequence=0)
    assert synced is not None
    assert synced.updates[0].value is None
    assert synced.updates[0].quality == "GoodNoData"
    await service.close()


@pytest.mark.asyncio
async def test_sync_reports_stream_active_when_connected() -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1)
    created = await service.create_subscription(client_id="c1", display_name="s1")
    subscription_id = created.subscription_id

    generation = await service.activate_stream("c1", subscription_id)
    assert generation == 1

    synced = await service.sync("c1", subscription_id, acknowledge_sequence=0)
    assert synced is not None
    assert synced.stream_active is True

    await service.deactivate_stream(subscription_id, generation)
    await service.close()


@pytest.mark.asyncio
async def test_sync_reports_queue_overflow_range() -> None:
    service = SubscriptionService(
        cast(OpcUaClientProtocol, FakeOpcUaClient()),
        interval_seconds=1,
        max_updates_per_subscription=2,
    )
    created = await service.create_subscription(client_id="c1", display_name="s1")
    subscription_id = created.subscription_id
    model = _model()
    assert await service.register_items("c1", subscription_id, ["asset-root"], max_depth=1, model=model) is True

    await service.handle_datachange(subscription_id, "ns=2;s=Temperature", 1.0)
    await service.handle_datachange(subscription_id, "ns=2;s=Pressure", 2.0)
    await service.handle_datachange(subscription_id, "ns=2;s=Temperature", 3.0)

    synced = await service.sync("c1", subscription_id, acknowledge_sequence=0)
    assert synced is not None
    assert synced.queue_overflow is True
    assert synced.dropped_from_sequence == 1
    assert synced.dropped_to_sequence == 1
    assert [item.sequence_number for item in synced.updates] == [2, 3]
    await service.close()


@pytest.mark.asyncio
async def test_subscription_ttl_expires_inactive_subscription() -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1, ttl_seconds=1)
    created = await service.create_subscription(client_id="c1", display_name="s1")
    subscription_id = created.subscription_id
    assert await service.register_items("c1", subscription_id, ["asset-root"], max_depth=0, model=_model())

    # Shift the activity stamp past the TTL instead of sleeping: the sweep compares
    # stamps against the loop clock, so driving it directly is deterministic.
    service._subscriptions[subscription_id].last_activity_monotonic -= 2.0
    await service._expire_subscriptions()

    synced = await service.sync("c1", subscription_id, acknowledge_sequence=0)
    assert synced is None
    assert not service._node_monitors
    assert not service._bins
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("subscription_count", [1, 3])
@pytest.mark.parametrize("keep_active_subscription", [False, True])
async def test_subscription_ttl_releases_expired_payloads(
    subscription_count: int, keep_active_subscription: bool
) -> None:
    class Payload:
        pass

    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=0.1, ttl_seconds=1)
    payload_refs: list[weakref.ReferenceType[Payload]] = []
    expired_ids: list[str] = []
    expiry_events: list[asyncio.Event] = []
    try:
        for _ in range(subscription_count):
            created = await service.create_subscription(client_id="c1", display_name="expired")
            expired_ids.append(created.subscription_id)
            state = service._subscriptions[created.subscription_id]
            expiry_events.append(state.update_event)
            payload = Payload()
            payload_refs.append(weakref.ref(payload))
            state.last_values_by_node_id["node1"] = payload
            state.updates.append(SubscriptionUpdate(1, "prop1", "node1", payload, "Good", ""))
            state.last_activity_monotonic -= 2
            del payload, state

        if keep_active_subscription:
            await service.create_subscription(client_id="c1", display_name="active")

        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in expiry_events)), timeout=2)

        # Weakrefs drop once the expiry coroutine's frame releases the payloads. Poll for
        # collection instead of a fixed sleep so the assertion cannot race the loop frames.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not all(payload_ref() is None for payload_ref in payload_refs):
            gc.collect()
            await asyncio.sleep(0.05)
        gc.collect()
        assert service._cleanup_task is not None
        assert not service._cleanup_task.done()
        assert all(subscription_id not in service._subscriptions for subscription_id in expired_ids)
        assert len(service._subscriptions) == int(keep_active_subscription)
        assert all(payload_ref() is None for payload_ref in payload_refs)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_subscription_ttl_ignores_server_callbacks() -> None:
    # Set TTL to 1 second
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=0.1, ttl_seconds=1)
    created = await service.create_subscription(client_id="c1", display_name="s1")
    subscription_id = created.subscription_id

    # Simulate frequent server-side callbacks. Each callback delivers a value but must not
    # refresh the client-activity stamp, so the TTL window is measured from the last client
    # interaction (create/sync/wait_for_updates), not from server traffic.
    for i in range(7):
        await service.handle_datachange(subscription_id, "node1", i)

    # Drive the sweep past the TTL by shifting the activity stamp (created at stamp=now)
    # instead of sleeping > 1s of wall time.
    service._subscriptions[subscription_id].last_activity_monotonic -= 2.0
    await service._expire_subscriptions()
    synced = await service.sync("c1", subscription_id, acknowledge_sequence=0)
    assert synced is None, "Subscription should have expired despite server callbacks"
    await service.close()


def test_collect_property_source_mappings_depth_limit() -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1)
    model = _model()
    mappings_depth1 = service._collect_property_source_mappings(model, model.nodes_by_id["asset-root"], max_depth=1)
    mappings_unbounded = service._collect_property_source_mappings(
        model,
        model.nodes_by_id["asset-root"],
        max_depth=0,
    )
    assert set(mappings_depth1.values()) == {"prop-a"}
    # Spec: recursion follows HasComponent only, so a hierarchical child's property is
    # never collected through the parent — even with unbounded depth.
    assert mappings_unbounded == {"ns=2;s=Temperature": "prop-a"}
    assert service._collect_property_source_mappings(model, model.nodes_by_id["asset-child"], max_depth=1) == {
        "ns=2;s=Pressure": "prop-b"
    }


@pytest.mark.asyncio
async def test_handle_datachange_ignores_unknown_nodes_for_multi_node_subscription() -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1)
    created = await service.create_subscription(client_id="c1", display_name="s1")
    subscription_id = created.subscription_id

    model = _model()
    assert await service.register_items("c1", subscription_id, ["asset-root"], max_depth=1, model=model) is True

    await service.handle_datachange(subscription_id, "ns=2;s=Unknown", 10.0)
    synced = await service.sync("c1", subscription_id, acknowledge_sequence=0)
    assert synced is not None
    assert all(item.node_id != "ns=2;s=Unknown" for item in synced.updates)
    await service.close()


@pytest.mark.asyncio
async def test_close_clears_subscriptions_and_unregisters_listener() -> None:
    fake_client = FakeOpcUaClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, fake_client), interval_seconds=1)
    created = await service.create_subscription(client_id="c1", display_name="s1")
    subscription_id = created.subscription_id

    model = _model()
    assert await service.register_items("c1", subscription_id, ["asset-root"], max_depth=1, model=model) is True

    await service.close()

    assert service._subscriptions == {}
    assert fake_client.removed_listeners


def test_append_update_deduplicates_same_value() -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1)
    state = _SubscriptionState(
        subscription_id="sub-1",
        client_id="c1",
        display_name=None,
        monitored_objects={},
        updates=[],
        sequence_number=0,
        node_to_element_id={"ns=2;s=Temperature": "prop-a"},
        update_event=asyncio.Event(),
        monitored_node_ids=set(),
    )
    service._append_update(state, "ns=2;s=Temperature", 1.0)
    service._append_update(state, "ns=2;s=Temperature", 1.0)
    service._append_update(state, "ns=2;s=Temperature", 2.0)
    assert len(state.updates) == 2
    parsed_timestamp = datetime.fromisoformat(state.updates[0].timestamp.replace("Z", "+00:00"))
    assert parsed_timestamp <= datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_distinct_nodes_are_packed_into_one_bin() -> None:
    """Phase 2: two distinct nodes with spare per-subscription capacity share one bin."""
    client = FakeOpcUaClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=1, seed_initial_values=False)
    created = await service.create_subscription(client_id="c1", display_name=None)
    subscription_id = created.subscription_id
    model = _model()

    assert (
        await service.register_items("c1", subscription_id, ["asset-root", "asset-child"], max_depth=2, model=model)
        is True
    )

    # Two distinct properties (prop-a, prop-b) resolved to two node monitors, but only
    # one native OPC UA subscription (bin) was opened since the fake server's capacity
    # (100) comfortably fits both.
    assert client.create_subscription_calls == 1

    async with service._lock:
        assert len(service._bins) == 1
        bin_ = next(iter(service._bins.values()))
        assert set(bin_.node_handles.keys()) == {"ns=2;s=Temperature", "ns=2;s=Pressure"}
        assert service._node_monitors["ns=2;s=Temperature"].bin_id == bin_.bin_id
        assert service._node_monitors["ns=2;s=Pressure"].bin_id == bin_.bin_id

    await service.close()


@pytest.mark.asyncio
async def test_concurrent_distinct_node_admissions_share_available_bin() -> None:
    class _DelayedAdmissionClient(FakeOpcUaClient):
        async def create_datachange_subscription(self, publishing_interval_ms: float, handler: Any) -> Any:
            await asyncio.sleep(0.01)
            return await super().create_datachange_subscription(publishing_interval_ms, handler)

    client = _DelayedAdmissionClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=1, seed_initial_values=False)
    try:
        await asyncio.gather(*(service._acquire_node_monitor(f"ns=1;i={index}", f"sub-{index}") for index in range(6)))

        assert client.create_subscription_calls == 1
        assert len(service._bins) == 1
        assert len(next(iter(service._bins.values())).node_handles) == 6
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("per_call", "per_subscription", "node_count", "expected_capacity"),
    [
        (1, 2, 2, 2),
        (1000, 100000, 1001, 100000),
        (1, None, 2, 1000),
        (None, 2, 2, 2),
    ],
)
async def test_bin_capacity_is_independent_of_per_call_limit(
    per_call: int | None,
    per_subscription: int | None,
    node_count: int,
    expected_capacity: int,
) -> None:
    class _CapacityClient(FakeOpcUaClient):
        async def get_subscription_capabilities(self) -> OpcUaSubscriptionCapabilities:
            return OpcUaSubscriptionCapabilities(
                max_monitored_items_per_call=per_call,
                max_subscriptions=100,
                max_monitored_items=1000000,
                max_subscriptions_per_session=10,
                max_monitored_items_per_subscription=per_subscription,
            )

        async def subscribe_data_changes(self, subscription: Any, node_ids: list[str]) -> int | list[int]:
            if per_call is not None:
                assert len(node_ids) <= per_call
            return await super().subscribe_data_changes(subscription, node_ids)

    client = _CapacityClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=1, seed_initial_values=False)
    try:
        for index in range(node_count):
            await service._acquire_node_monitor(f"ns=1;i={index}", "sub-test")

        assert client.create_subscription_calls == 1
        assert len(service._bins) == 1
        bin_ = next(iter(service._bins.values()))
        assert bin_.capacity == expected_capacity
        assert len(bin_.node_handles) == node_count
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_total_monitored_item_limit_applies_to_existing_bin() -> None:
    class _LimitedClient(FakeOpcUaClient):
        async def get_subscription_capabilities(self) -> OpcUaSubscriptionCapabilities:
            return OpcUaSubscriptionCapabilities(
                max_monitored_items_per_call=1,
                max_subscriptions=100,
                max_monitored_items=2,
                max_subscriptions_per_session=10,
                max_monitored_items_per_subscription=100000,
            )

    client = _LimitedClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=1, seed_initial_values=False)
    try:
        for index in range(3):
            await service._acquire_node_monitor(f"ns=1;i={index}", "sub-test")

        assert client.create_subscription_calls == 1
        assert client.subscribe_calls == 2
        assert len(next(iter(service._bins.values())).node_handles) == 2
        assert service._node_monitors["ns=1;i=2"].mode == "polling"
        assert service._node_monitors["ns=1;i=2"].bin_id is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_bin_overflow_opens_second_bin() -> None:
    """Phase 2: once a bin is full, the next node opens a new bin instead of failing."""

    class _TinyCapacityClient(FakeOpcUaClient):
        async def get_subscription_capabilities(self) -> Any:
            return SimpleNamespace(
                max_monitored_items_per_call=1,
                max_subscriptions=10,
                max_monitored_items=10,
                max_subscriptions_per_session=10,
                max_monitored_items_per_subscription=1,
            )

    client = _TinyCapacityClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=1, seed_initial_values=False)
    created = await service.create_subscription(client_id="c1", display_name=None)
    subscription_id = created.subscription_id
    model = _model()

    assert (
        await service.register_items("c1", subscription_id, ["asset-root", "asset-child"], max_depth=2, model=model)
        is True
    )

    # Capacity of 1 monitored item per bin forces the two distinct nodes into two bins.
    assert client.create_subscription_calls == 2
    async with service._lock:
        assert len(service._bins) == 2
        assert all(len(bin_.node_handles) == 1 for bin_ in service._bins.values())

    await service.close()


@pytest.mark.asyncio
async def test_bin_reclaimed_when_last_node_released_others_kept() -> None:
    """Phase 2: releasing one node from a shared bin keeps the bin for the remaining node."""
    client = FakeOpcUaClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=1, seed_initial_values=False)
    created = await service.create_subscription(client_id="c1", display_name=None)
    subscription_id = created.subscription_id
    model = _model()

    assert await service.register_items("c1", subscription_id, ["prop-a", "prop-b"], max_depth=0, model=model) is True
    async with service._lock:
        bin_id = service._node_monitors["ns=2;s=Temperature"].bin_id

    # Drop just prop-a; prop-b (same bin) must stay monitored, and only that one
    # monitored item is unsubscribed rather than tearing down the whole bin.
    assert await service.unregister_items("c1", subscription_id, ["prop-a"], model=model) is True
    async with service._lock:
        assert bin_id in service._bins
        assert "ns=2;s=Temperature" not in service._bins[bin_id].node_handles
        assert "ns=2;s=Pressure" in service._bins[bin_id].node_handles
    assert client.unsubscribed_calls
    assert not client.deleted_subscriptions

    # Dropping the last node in the bin reclaims (deletes) the whole OPC UA subscription.
    assert await service.unregister_items("c1", subscription_id, ["prop-b"], model=model) is True
    async with service._lock:
        assert bin_id not in service._bins
    assert len(client.deleted_subscriptions) == 1

    await service.close()


@pytest.mark.asyncio
async def test_concurrent_register_unregister_leaves_no_orphan_monitor() -> None:
    """Applied-vs-desired reconciliation must survive concurrent churn on the same subscription."""
    client = FakeOpcUaClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=1, seed_initial_values=False)
    created = await service.create_subscription(client_id="c1", display_name=None)
    subscription_id = created.subscription_id
    model = _model()

    await asyncio.gather(
        service.register_items("c1", subscription_id, ["prop-a"], max_depth=0, model=model),
        service.unregister_items("c1", subscription_id, ["prop-a"], model=model),
        service.register_items("c1", subscription_id, ["prop-b"], max_depth=0, model=model),
        service.unregister_items("c1", subscription_id, ["prop-b"], model=model),
    )

    async with service._lock:
        state = service._subscriptions[subscription_id]
        assert state.monitored_node_ids == set()
        # Every acquisition was reconciled again, so nothing is left applied.
        assert state.applied_node_ids == set()
        assert service._node_monitors == {}

    await service.delete_subscriptions("c1", [subscription_id])
    await service.close()


@pytest.mark.asyncio
async def test_delete_releases_applied_nodes_outside_the_desired_set() -> None:
    """Deletion must release nodes that were acquired but no longer desired."""
    client = FakeOpcUaClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=1, seed_initial_values=False)
    created = await service.create_subscription(client_id="c1", display_name=None)
    subscription_id = created.subscription_id
    model = _model()
    assert await service.register_items("c1", subscription_id, ["prop-a"], max_depth=0, model=model) is True

    async with service._lock:
        state = service._subscriptions[subscription_id]
        # Simulate the divergent state the old snapshot-based diff could produce.
        state.monitored_node_ids = set()
        state.applied_node_ids = {"ns=2;s=Temperature"}

    await service.delete_subscriptions("c1", [subscription_id])

    async with service._lock:
        assert service._node_monitors == {}
    await service.close()


@pytest.mark.asyncio
async def test_reconnect_keeps_restored_bin_and_adopts_new_server_handles() -> None:
    client = FakeOpcUaClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=1, seed_initial_values=False)
    created = await service.create_subscription(client_id="c1", display_name=None)
    model = _model()
    assert await service.register_items(
        "c1",
        created.subscription_id,
        ["asset-root", "asset-child"],
        max_depth=2,
        model=model,
    )

    async with service._lock:
        bin_ = next(iter(service._bins.values()))
    client.restored_handles[id(bin_.ua_subscription)] = {"ns=2;s=Temperature": 501, "ns=2;s=Pressure": 502}

    await service._handle_client_reconnect()

    async with service._lock:
        assert list(service._bins) == [bin_.bin_id]
        assert bin_.node_handles == {"ns=2;s=Temperature": 501, "ns=2;s=Pressure": 502}
        assert service._node_monitors["ns=2;s=Temperature"].mode == "native"
    assert client.create_subscription_calls == 1
    assert not client.deleted_subscriptions

    await service.close()


@pytest.mark.asyncio
async def test_reconnect_readmits_items_asyncua_could_not_restore() -> None:
    client = FakeOpcUaClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=1, seed_initial_values=False)
    created = await service.create_subscription(client_id="c1", display_name=None)
    model = _model()
    assert await service.register_items(
        "c1",
        created.subscription_id,
        ["asset-root", "asset-child"],
        max_depth=2,
        model=model,
    )

    async with service._lock:
        old_bin = next(iter(service._bins.values()))
    # Pressure was dropped by the server during recreate; Temperature survived.
    client.restored_handles[id(old_bin.ua_subscription)] = {"ns=2;s=Temperature": 601}

    await service._handle_client_reconnect()

    async with service._lock:
        assert old_bin.node_handles["ns=2;s=Temperature"] == 601
        assert service._node_monitors["ns=2;s=Temperature"].bin_id == old_bin.bin_id
        pressure = service._node_monitors["ns=2;s=Pressure"]
        assert pressure.mode == "native"
        assert pressure.bin_id == old_bin.bin_id
        assert "ns=2;s=Pressure" in old_bin.node_handles

    # Whole subscription lost: the bin is dropped and its nodes re-admitted into a new one.
    client.restored_handles[id(old_bin.ua_subscription)] = None
    await service._handle_client_reconnect()

    async with service._lock:
        assert old_bin.bin_id not in service._bins
        assert len(service._bins) == 1
        new_bin = next(iter(service._bins.values()))
        assert set(new_bin.node_handles) == {"ns=2;s=Temperature", "ns=2;s=Pressure"}
    assert client.deleted_subscriptions == [old_bin.ua_subscription]

    await service.close()


@pytest.mark.asyncio
async def test_native_admission_failure_backs_off_to_polling() -> None:
    """Phase 4: a real OPC UA rejection triggers a cooldown so later admissions poll instead of retrying native."""

    class _FailingCreateClient(FakeOpcUaClient):
        async def create_datachange_subscription(self, publishing_interval_ms: float, handler: Any) -> Any:
            del publishing_interval_ms, handler
            raise _status_error("BadTooManySubscriptions")

    client = _FailingCreateClient()
    service = SubscriptionService(
        cast(OpcUaClientProtocol, client),
        interval_seconds=1,
        seed_initial_values=False,
        native_backoff_seconds=60.0,
    )
    created_a = await service.create_subscription(client_id="c1", display_name=None)
    model = _model()
    assert await service.register_items("c1", created_a.subscription_id, ["prop-a"], max_depth=0, model=model) is True

    async with service._lock:
        assert service._node_monitors["ns=2;s=Temperature"].mode == "polling"
    assert service._native_backoff_until_monotonic > 0.0

    # A second, unrelated node registered shortly after should skip native entirely
    # (no further create_datachange_subscription attempts) while backoff is active.
    attempts_before = 0

    async def _count_and_fail(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal attempts_before
        attempts_before += 1
        raise RuntimeError("should not be retried during backoff")

    client.create_datachange_subscription = _count_and_fail  # type: ignore[method-assign]

    created_b = await service.create_subscription(client_id="c2", display_name=None)
    assert await service.register_items("c2", created_b.subscription_id, ["prop-b"], max_depth=0, model=model) is True
    async with service._lock:
        assert service._node_monitors["ns=2;s=Pressure"].mode == "polling"
    assert attempts_before == 0

    await service.close()


def _status_error(status_name: str) -> UaStatusCodeError:
    code = getattr(StatusCodes, status_name)
    value = int(code.value) if hasattr(code, "value") else int(code)
    return UaStatusCodeError(value)  # type: ignore[no-untyped-call]


@pytest.mark.asyncio
async def test_per_item_rejection_degrades_only_that_node_and_cleans_up_subscription() -> None:
    """A rejected monitored item must not back off the whole service and must not leak its subscription.

    ``subscribe_data_changes`` raises ``UaStatusCodeError`` for an unsupported node after the OPC UA
    subscription already exists: asyncua keeps no registry of it, so it has to be deleted here, and the
    remaining nodes must keep monitoring natively.
    """

    class _RejectingAddClient(FakeOpcUaClient):
        rejected_node_ids: set[str]

        def __init__(self) -> None:
            super().__init__()
            self.rejected_node_ids = {"ns=2;s=Temperature"}

        async def subscribe_data_changes(self, subscription: Any, node_ids: list[str]) -> int | list[int]:
            if node_ids[0] in self.rejected_node_ids:
                raise _status_error("BadNotSupported")
            return await super().subscribe_data_changes(subscription, node_ids)

    client = _RejectingAddClient()
    service = SubscriptionService(
        cast(OpcUaClientProtocol, client),
        interval_seconds=1,
        seed_initial_values=False,
        native_backoff_seconds=60.0,
    )
    created = await service.create_subscription(client_id="c1", display_name=None)
    model = _model()
    assert await service.register_items("c1", created.subscription_id, ["prop-a"], max_depth=0, model=model) is True

    async with service._lock:
        assert service._node_monitors["ns=2;s=Temperature"].mode == "polling"
        assert not service._bins
    # The subscription created for the rejected item was deleted again.
    assert len(client.deleted_subscriptions) == 1
    # A per-item rejection is not a session-level limit: no service-wide backoff.
    assert service._native_backoff_until_monotonic == 0.0

    # Other nodes still go native - the rejection must not degrade them for 60s.
    assert await service.register_items("c1", created.subscription_id, ["prop-b"], max_depth=0, model=model) is True
    async with service._lock:
        assert service._node_monitors["ns=2;s=Pressure"].mode == "native"
        assert client.create_subscription_calls == 2

    await service.close()


@pytest.mark.asyncio
async def test_wait_for_updates_cancels_its_futures_on_client_disconnect() -> None:
    """Disconnecting an SSE stream must not leave the wakeup/abort futures pending."""

    client = FakeOpcUaClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=1, seed_initial_values=False)
    model = _model()
    created = await service.create_subscription(client_id="c1", display_name=None)
    assert await service.register_items("c1", created.subscription_id, ["prop-a"], max_depth=0, model=model) is True

    baseline = len(asyncio.all_tasks())
    wait_task = asyncio.create_task(
        service.wait_for_updates("c1", created.subscription_id, after_sequence=0, timeout_seconds=30)
    )
    await asyncio.sleep(0.05)
    wait_task.cancel()
    with suppress(asyncio.CancelledError):
        await wait_task

    # No orphaned futures: only the service's own background tasks remain.
    assert len(asyncio.all_tasks()) == baseline
    await service.close()


@pytest.mark.asyncio
async def test_close_prevents_resurrecting_background_tasks() -> None:
    """Post-close calls must not restart the background runtime against a disconnecting client."""

    client = FakeOpcUaClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=1)
    await service.close()

    service._ensure_cleanup_task()
    assert service._cleanup_task is None
    assert service._node_datachange_task is None
    assert service._shared_polling_task is None

    # Reconnect callbacks and sync after close must be no-ops rather than re-admitting nodes.
    await service._handle_client_reconnect()
    assert client.create_subscription_calls == 0
    assert await service.sync("c1", "sub-does-not-exist", acknowledge_sequence=None) is None


@pytest.mark.asyncio
async def test_sync_with_active_stream_refreshes_ttl() -> None:
    """Polling while a stream is open keeps the subscription alive instead of expiring it."""

    client = FakeOpcUaClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=1, ttl_seconds=1)
    model = _model()
    created = await service.create_subscription(client_id="c1", display_name=None)
    assert await service.register_items("c1", created.subscription_id, ["prop-a"], max_depth=0, model=model) is True
    generation = await service.activate_stream("c1", created.subscription_id)
    assert generation is not None

    async with service._lock:
        state = service._subscriptions[created.subscription_id]
        before = state.last_activity_monotonic

    result = await service.sync("c1", created.subscription_id, acknowledge_sequence=None)
    assert result is not None
    assert result.stream_active is True

    async with service._lock:
        assert service._subscriptions[created.subscription_id].last_activity_monotonic > before

    await service.close()


@pytest.mark.asyncio
async def test_initial_snapshot_reports_server_status_not_good_nodata() -> None:
    client = FakeOpcUaClient()
    client.data_value_overrides["ns=2;s=Temperature"] = _signed_dto(
        None, 0x80000000, datetime(2026, 2, 3, 4, 5, tzinfo=timezone.utc)
    )
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=1, seed_initial_values=True)
    created = await service.create_subscription(client_id="c1", display_name="s1")
    assert (
        await service.register_items("c1", created.subscription_id, ["asset-root"], max_depth=1, model=_model()) is True
    )

    synced = await service.sync("c1", created.subscription_id, acknowledge_sequence=None)
    assert synced is not None
    assert [item.element_id for item in synced.updates] == ["prop-a"]
    assert synced.updates[0].quality == "Bad"
    assert synced.updates[0].value is None
    assert synced.updates[0].timestamp == "2026-02-03T04:05:00Z"
    await service.close()


@pytest.mark.asyncio
async def test_shared_polling_loop_maps_server_status_and_timestamp() -> None:
    client = FakeOpcUaClient()

    def _no_native_subscriptions(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("native subscriptions unavailable")

    client.create_datachange_subscription = _no_native_subscriptions  # type: ignore[method-assign]
    client.data_value_overrides["ns=2;s=Temperature"] = _signed_dto(
        7.0, 0x40000000, datetime(2026, 3, 4, 5, 6, tzinfo=timezone.utc)
    )

    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=0.1, seed_initial_values=False)
    created = await service.create_subscription(client_id="c1", display_name="s1")
    assert (
        await service.register_items("c1", created.subscription_id, ["asset-root"], max_depth=1, model=_model()) is True
    )
    async with service._lock:
        assert service._node_monitors["ns=2;s=Temperature"].mode == "polling"

    updates = await service.wait_for_updates("c1", created.subscription_id, after_sequence=0, timeout_seconds=2)
    assert updates is not None
    assert updates[0].element_id == "prop-a"
    assert updates[0].value == 7.0
    assert updates[0].quality == "Uncertain"
    assert updates[0].timestamp == "2026-03-04T05:06:00Z"
    await service.close()


@pytest.mark.asyncio
async def test_native_handler_maps_status_code_and_source_timestamp() -> None:
    client = FakeOpcUaClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=1, seed_initial_values=False)
    created = await service.create_subscription(client_id="c1", display_name="s1")
    assert (
        await service.register_items("c1", created.subscription_id, ["asset-root"], max_depth=1, model=_model()) is True
    )
    assert client.handlers

    # The fake hands the port callback straight to the service (the real client wraps it in
    # the adapter's notification bridge, exercised in test_opcua_dto_ports.py).
    client.handlers[0].datachange_notification(
        "ns=2;s=Temperature", None, _signed_dto(None, 0x80000000, datetime(2026, 4, 5, 6, 7, tzinfo=timezone.utc))
    )

    updates = await service.wait_for_updates("c1", created.subscription_id, after_sequence=0, timeout_seconds=2)
    assert updates is not None
    assert updates[0].element_id == "prop-a"
    assert updates[0].quality == "Bad"
    assert updates[0].value is None
    assert updates[0].timestamp == "2026-04-05T06:07:00Z"
    await service.close()


def test_quality_change_emits_an_update_even_with_an_unchanged_value() -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1.0)
    state = _SubscriptionState(
        subscription_id="sub-quality",
        client_id="client",
        display_name=None,
        monitored_objects={"prop-a": 0},
        node_to_element_id={"ns=2;s=Temperature": "prop-a"},
    )
    service._append_update(state, "ns=2;s=Temperature", 1.0, _ValueSignal("Good", "2026-01-01T00:00:00Z"))
    service._append_update(state, "ns=2;s=Temperature", 1.0, _ValueSignal("Uncertain", "2026-01-01T00:00:01Z"))
    service._append_update(state, "ns=2;s=Temperature", 1.0, _ValueSignal("Uncertain", "2026-01-01T00:00:02Z"))

    assert [(item.quality, item.timestamp) for item in state.updates] == [
        ("Good", "2026-01-01T00:00:00Z"),
        ("Uncertain", "2026-01-01T00:00:01Z"),
    ]


def test_null_value_with_good_or_uncertain_quality_is_normalized_to_good_nodata() -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1.0)
    state = _SubscriptionState(
        subscription_id="sub-null",
        client_id="client",
        display_name=None,
        monitored_objects={"prop-a": 0},
        node_to_element_id={"ns=2;s=Temperature": "prop-a"},
    )
    service._append_update(state, "ns=2;s=Temperature", None, _ValueSignal("Good", None))
    service._append_update(state, "ns=2;s=Temperature", None, _ValueSignal("Bad", None))

    assert [item.quality for item in state.updates] == ["GoodNoData", "Bad"]


@pytest.mark.asyncio
async def test_cursor_delivery_survives_buffer_eviction() -> None:
    """The tail-walk returns exactly the entries newer than the cursor, even after drops."""
    service = SubscriptionService(
        cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1, max_updates_per_subscription=3
    )
    created = await service.create_subscription(client_id="c1")
    subscription_id = created.subscription_id
    state_obj = service._subscriptions[subscription_id]
    for value in (1.0, 2.0, 3.0, 4.0, 5.0):
        service._append_update(state_obj, "ns=2;s=Temperature", value)

    # Sequences 1 and 2 were evicted; the buffer keeps 3..5 with the overflow markers.
    assert [item.sequence_number for item in state_obj.updates] == [3, 4, 5]
    assert state_obj.dropped_from_sequence == 1
    assert state_obj.dropped_to_sequence == 2

    assert [item.sequence_number for item in service._updates_after(state_obj, 2)] == [3, 4, 5]
    assert [item.sequence_number for item in service._updates_after(state_obj, 4)] == [5]
    assert service._updates_after(state_obj, 5) == []
    # A cursor ahead of the buffer (consumed then evicted) delivers nothing, not the buffer.
    assert service._updates_after(state_obj, 99) == []

    delivered = await service.wait_for_updates("c1", subscription_id, after_sequence=2, timeout_seconds=0.05)
    assert delivered is not None
    assert [item.sequence_number for item in delivered] == [3, 4, 5]
    await service.close()


@pytest.mark.asyncio
async def test_sync_ack_trims_head_and_keeps_order() -> None:
    """Acknowledging removes the consumed prefix; entries past the ack stay untouched."""
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1)
    created = await service.create_subscription(client_id="c1")
    subscription_id = created.subscription_id
    state_obj = service._subscriptions[subscription_id]
    for value in range(1, 5):
        service._append_update(state_obj, "ns=2;s=Temperature", float(value))

    synced = await service.sync("c1", subscription_id, acknowledge_sequence=2)
    assert synced is not None
    assert [item.sequence_number for item in synced.updates] == [3, 4]
    assert [item.sequence_number for item in state_obj.updates] == [3, 4]

    # Acking the last buffered sequence empties the buffer; acks beyond the sequence counter
    # stay a no-op (the counter only moves with new appends).
    synced_all = await service.sync("c1", subscription_id, acknowledge_sequence=4)
    assert synced_all is not None
    assert synced_all.updates == []
    assert not state_obj.updates
    await service.close()


@pytest.mark.asyncio
async def test_fanout_delivers_to_every_subscriber() -> None:
    """One node-level change reaches all subscriptions with per-stream sequences."""
    service = SubscriptionService(
        cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1, seed_initial_values=False
    )
    sub_a = await service.create_subscription(client_id="client-a")
    sub_b = await service.create_subscription(client_id="client-b")
    # A subscriber that monitors nothing (and a vanished one) must be skipped, not delivered to.
    sub_c = await service.create_subscription(client_id="client-c")
    node_id = "ns=2;s=Temperature"
    monitor = _NodeMonitor(
        node_id=node_id,
        subscriber_ids={sub_a.subscription_id, sub_b.subscription_id, sub_c.subscription_id, "sub-dead"},
    )
    async with service._lock:
        service._node_monitors[node_id] = monitor
        for subscription_id in (sub_a.subscription_id, sub_b.subscription_id):
            subscription_state = service._subscriptions[subscription_id]
            subscription_state.monitored_objects = {"prop-a": 0}
            subscription_state.monitored_node_ids = {node_id}
            subscription_state.node_to_element_id = {node_id: "prop-a"}

    await service._fanout_node_value(node_id, 42.0)
    await service._fanout_node_value(node_id, 43.0)

    synced_a = await service.sync("client-a", sub_a.subscription_id)
    synced_b = await service.sync("client-b", sub_b.subscription_id)
    assert synced_a is not None and synced_b is not None
    expected = [("prop-a", 42.0, 1), ("prop-a", 43.0, 2)]
    assert [(item.element_id, item.value, item.sequence_number) for item in synced_a.updates] == expected
    assert [(item.element_id, item.value, item.sequence_number) for item in synced_b.updates] == expected

    synced_c = await service.sync("client-c", sub_c.subscription_id)
    assert synced_c is not None and synced_c.updates == []
    await service.close()


@pytest.mark.asyncio
async def test_concurrent_fanout_and_delivery_keep_sequences_ordered() -> None:
    """Interleaved deliveries and polling across several subscriptions keep per-stream order."""
    service = SubscriptionService(
        cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1, seed_initial_values=False
    )
    subs = [await service.create_subscription(client_id=f"c{i}") for i in range(3)]
    node_id = "ns=2;s=Temperature"
    monitor = _NodeMonitor(node_id=node_id, subscriber_ids={sub.subscription_id for sub in subs})
    async with service._lock:
        service._node_monitors[node_id] = monitor
        for detail in subs:
            subscription_state = service._subscriptions[detail.subscription_id]
            subscription_state.monitored_objects = {"prop-a": 0}
            subscription_state.monitored_node_ids = {node_id}
            subscription_state.node_to_element_id = {node_id: "prop-a"}

    async def tick(value: float) -> None:
        await service._fanout_node_value(node_id, value)

    await asyncio.wait_for(asyncio.gather(*(tick(float(i)) for i in range(1, 21))), timeout=5)

    for detail in subs:
        synced = await service.sync(detail.client_id, detail.subscription_id)
        assert synced is not None
        sequences = [item.sequence_number for item in synced.updates]
        assert sequences == list(range(1, 21))
    await service.close()


@pytest.mark.asyncio
async def test_release_node_monitor_discards_queued_drop_counts() -> None:
    """Drop counts for a node nobody monitors must not fabricate gaps or leak entries."""
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1)
    node_id = "ns=2;s=Temperature"
    queue = service._node_datachange_queue
    # Fill the shared queue and push further entries: the overflow is remembered per node.
    for _ in range(queue.maxsize + 2):
        service._enqueue_node_datachange(node_id, 1.0)
    assert service._dropped_node_datachanges.get(node_id, 0) >= 1

    created = await service.create_subscription(client_id="c1")
    async with service._lock:
        monitor = _NodeMonitor(node_id=node_id, subscriber_ids={created.subscription_id})
        service._node_monitors[node_id] = monitor
    await service._release_node_monitor(node_id, created.subscription_id)
    assert node_id not in service._dropped_node_datachanges
    await service.close()
