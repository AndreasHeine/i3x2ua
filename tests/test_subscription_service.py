from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest

from i3x_server.infrastructure.opcua.client import (
    OpcUaClientProtocol,
    OpcUaSubscriptionCapabilities,
)
from i3x_server.infrastructure.subscriptions.service import (
    SubscriptionService,
    _DataChangeHandler,
    _ManagedBin,
    _min_positive,
    _NodeMonitor,
    _SubscriptionState,
)
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult


class FakeOpcUaClient:
    def __init__(self) -> None:
        self.listeners: list[Any] = []
        self.removed_listeners: list[Any] = []
        self.deleted_subscriptions: list[Any] = []
        self.read_values_calls = 0
        self.create_subscription_calls = 0
        self.subscribe_calls = 0
        self.unsubscribed_calls: list[tuple[Any, list[int]]] = []

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
        del publishing_interval_ms, handler
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

    async def read_values(self, node_ids: list[str]) -> list[float]:
        self.read_values_calls += 1
        return [float(i + self.read_values_calls) for i, _ in enumerate(node_ids)]


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
        instances_by_type_id={},
        property_to_node={prop_a.id: prop_a.source_node_id, prop_b.id: prop_b.source_node_id},
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
    assert await service.register_items("c1", subscription_id, ["asset-root"], max_depth=2, model=model) is True

    first = await service.sync("c1", subscription_id, acknowledge_sequence=0)
    assert first is not None
    assert [item.sequence_number for item in first.updates] == [1, 2]

    second = await service.sync("c1", subscription_id, acknowledge_sequence=2)
    assert second is not None
    assert second.updates == []
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
    assert client.read_values_calls >= 2
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
async def test_unregister_unknown_and_updates_after_missing() -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1)
    model = _model()
    assert await service.unregister_items("c1", "missing", ["asset-root"], model=model) is False
    assert await service.updates_after("missing", 0) is None
    await service.close()


@pytest.mark.asyncio
async def test_handle_datachange_resolves_client_handle_mapping() -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1)
    created = await service.create_subscription(client_id="c1", display_name=None)
    subscription_id = created.subscription_id

    async with service._lock:
        state = service._subscriptions[subscription_id]
        state.handle_to_node_id[7] = "ns=2;s=Temperature"
        state.node_to_element_id["ns=2;s=Temperature"] = "prop-a"

    await service.handle_datachange(subscription_id, "ignored-node", 5.0, client_handle=7)
    synced = await service.sync("c1", subscription_id, acknowledge_sequence=0)
    assert synced is not None
    assert synced.updates[0].element_id == "prop-a"
    await service.close()


@pytest.mark.asyncio
async def test_datachange_handler_schedules_from_non_eventloop_thread() -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1)
    created = await service.create_subscription(client_id="c1", display_name=None)
    subscription_id = created.subscription_id

    async with service._lock:
        state = service._subscriptions[subscription_id]
        state.node_to_element_id["ns=2;s=Temperature"] = "prop-a"

    handler = _DataChangeHandler(service, subscription_id)
    fake_node = SimpleNamespace(nodeid=SimpleNamespace(to_string=lambda: "ns=2;s=Temperature"))
    fake_data = SimpleNamespace(monitored_item=SimpleNamespace(ClientHandle=7))

    await asyncio.to_thread(handler.datachange_notification, fake_node, 23.5, fake_data)

    for _ in range(20):
        synced = await service.sync("c1", subscription_id, acknowledge_sequence=0)
        assert synced is not None
        if synced.updates:
            break
        await asyncio.sleep(0.01)

    assert synced is not None
    assert synced.updates
    assert synced.updates[0].element_id == "prop-a"
    await service.close()


@pytest.mark.asyncio
async def test_datachange_handler_schedules_from_other_running_loop() -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1)
    created = await service.create_subscription(client_id="c1", display_name=None)
    subscription_id = created.subscription_id

    async with service._lock:
        state = service._subscriptions[subscription_id]
        state.node_to_element_id["ns=2;s=Temperature"] = "prop-a"

    handler = _DataChangeHandler(service, subscription_id)
    fake_node = SimpleNamespace(nodeid=SimpleNamespace(to_string=lambda: "ns=2;s=Temperature"))
    fake_data = SimpleNamespace(monitored_item=SimpleNamespace(ClientHandle=7))

    def _invoke_from_other_loop() -> None:
        async def _run() -> None:
            handler.datachange_notification(fake_node, 24.5, fake_data)
            await asyncio.sleep(0.01)

        asyncio.run(_run())

    await asyncio.to_thread(_invoke_from_other_loop)

    for _ in range(20):
        synced = await service.sync("c1", subscription_id, acknowledge_sequence=0)
        assert synced is not None
        if synced.updates:
            break
        await asyncio.sleep(0.01)

    assert synced is not None
    assert synced.updates
    assert synced.updates[0].element_id == "prop-a"
    await service.close()


class _IntLikeHandle:
    def __init__(self, value: int) -> None:
        self._value = value

    def __int__(self) -> int:
        return self._value


class _SingleHandleOpcUaClient(FakeOpcUaClient):
    async def subscribe_data_changes(self, subscription: Any, node_ids: list[str]) -> int:
        del subscription, node_ids
        return 11


@pytest.mark.asyncio
async def test_datachange_handler_accepts_int_like_client_handle() -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1)
    created = await service.create_subscription(client_id="c1", display_name=None)
    subscription_id = created.subscription_id

    async with service._lock:
        state = service._subscriptions[subscription_id]
        state.handle_to_node_id[11] = "ns=2;s=Temperature"
        state.node_to_element_id["ns=2;s=Temperature"] = "prop-a"

    handler = _DataChangeHandler(service, subscription_id)
    fake_node = SimpleNamespace(nodeid=SimpleNamespace(to_string=lambda: "ns=2;s=DifferentFormat"))
    fake_data = SimpleNamespace(monitored_item=SimpleNamespace(ClientHandle=_IntLikeHandle(11)))
    handler.datachange_notification(fake_node, 31.2, fake_data)

    for _ in range(20):
        synced = await service.sync("c1", subscription_id, acknowledge_sequence=0)
        assert synced is not None
        if synced.updates:
            break
        await asyncio.sleep(0.01)

    assert synced is not None
    assert synced.updates
    assert synced.updates[0].element_id == "prop-a"
    await service.close()


@pytest.mark.asyncio
async def test_single_monitored_node_maps_single_int_handle() -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, _SingleHandleOpcUaClient()), interval_seconds=1)
    created = await service.create_subscription(client_id="c1", display_name=None)
    subscription_id = created.subscription_id

    model = _model()
    assert await service.register_items("c1", subscription_id, ["prop-a"], max_depth=1, model=model) is True

    await service.handle_datachange(subscription_id, "ns=2;s=Unmapped", 44.2, client_handle=11)
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
            capacity=1,
            node_handles={"ns=2;s=Existing": 1},
        )

    caps = OpcUaSubscriptionCapabilities(
        max_monitored_items_per_call=1,
        max_subscriptions=1,
        max_monitored_items=2,
        max_subscriptions_per_session=1,
        max_monitored_items_per_subscription=1,
    )
    assert await service._can_open_new_bin(caps) is False
    assert _min_positive(None, 0, -1) is None
    assert _min_positive(None, 5, 2) == 2
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

    await asyncio.sleep(2.2)

    synced = await service.sync("c1", subscription_id, acknowledge_sequence=0)
    assert synced is None
    await service.close()


@pytest.mark.asyncio
async def test_subscription_ttl_ignores_server_callbacks() -> None:
    # Set TTL to 1 second
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=0.1, ttl_seconds=1)
    created = await service.create_subscription(client_id="c1", display_name="s1")
    subscription_id = created.subscription_id

    # Simulate frequent server-side callbacks (every 0.2s for 1.5s total)
    # Total time > TTL, but each gap < TTL.
    for i in range(7):
        service.schedule_datachange(subscription_id, "node1", i, client_handle=None)
        await asyncio.sleep(0.2)

    # If the fix works, the subscription should now be considered stale
    # because the client hasn't called sync/wait_for_updates for > 1s,
    # even though server updates were arriving.
    await asyncio.sleep(0.5)  # Ensure cleanup loop has run at least once since last sleep
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
    assert mappings_unbounded == {
        "ns=2;s=Temperature": "prop-a",
        "ns=2;s=Pressure": "prop-b",
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
        handle_to_node_id={},
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

    assert await service.register_items("c1", subscription_id, ["asset-root"], max_depth=2, model=model) is True

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

    assert await service.register_items("c1", subscription_id, ["asset-root"], max_depth=2, model=model) is True

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
async def test_native_admission_failure_backs_off_to_polling() -> None:
    """Phase 4: a real OPC UA rejection triggers a cooldown so later admissions poll instead of retrying native."""

    class _FailingCreateClient(FakeOpcUaClient):
        async def create_datachange_subscription(self, publishing_interval_ms: float, handler: Any) -> Any:
            del publishing_interval_ms, handler
            raise RuntimeError("simulated ServiceFault: BadTooManySubscriptions")

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
