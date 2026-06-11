from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest

from i3x_server.opcua.client import (
    OpcUaClientProtocol,
    OpcUaSubscriptionCapabilities,
)
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult
from i3x_server.subscriptions.service import (
    SubscriptionService,
    _min_positive,
    _SubscriptionState,
)


class FakeOpcUaClient:
    def __init__(self) -> None:
        self.listeners: list[Any] = []
        self.deleted_subscriptions: list[Any] = []
        self.read_values_calls = 0

    def add_reconnect_listener(self, listener: Any) -> None:
        self.listeners.append(listener)

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
        return SimpleNamespace(id="ua-sub")

    async def subscribe_data_changes(self, subscription: Any, node_ids: list[str]) -> list[int]:
        del subscription, node_ids
        return [1, 2, 3]

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


@pytest.mark.asyncio
async def test_unregister_unknown_and_updates_after_missing() -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1)
    model = _model()
    assert await service.unregister_items("c1", "missing", ["asset-root"], model=model) is False
    assert await service.updates_after("missing", 0) is None


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


@pytest.mark.asyncio
async def test_polling_path_collects_updates() -> None:
    client = FakeOpcUaClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=1)
    created = await service.create_subscription(client_id="c1", display_name="poll")
    subscription_id = created.subscription_id

    model = _model()
    await service.register_items("c1", subscription_id, ["asset-root"], max_depth=2, model=model)

    async with service._lock:
        state = service._subscriptions[subscription_id]
        state.mode = "polling"

    task = asyncio.create_task(service._polling_loop(subscription_id))
    await asyncio.sleep(0.05)
    task.cancel()
    await task

    synced = await service.sync("c1", subscription_id, acknowledge_sequence=0)
    assert synced is not None
    assert len(synced.updates) >= 1


@pytest.mark.asyncio
async def test_must_use_polling_limits_and_helpers() -> None:
    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=1)
    created = await service.create_subscription(client_id="c1", display_name=None)
    subscription_id = created.subscription_id
    model = _model()
    await service.register_items("c1", subscription_id, ["asset-root"], max_depth=0, model=model)

    async with service._lock:
        state = service._subscriptions[subscription_id]
        state.monitored_node_ids = {"a", "b", "c"}

    caps = OpcUaSubscriptionCapabilities(
        max_monitored_items_per_call=1,
        max_subscriptions=1,
        max_monitored_items=2,
        max_subscriptions_per_session=1,
        max_monitored_items_per_subscription=1,
    )
    assert await service._must_use_polling(state, caps) is True
    assert _min_positive(None, 0, -1) is None
    assert _min_positive(None, 5, 2) == 2


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
    assert mappings_unbounded == {}


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
