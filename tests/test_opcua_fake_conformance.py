"""Behavioral conformance of the canonical shared OPC UA test double (L16).

The port-level doubles used to fork per test module: the conftest double, the API double, and
the subscription-service double diverged in completeness (one lacked reconnect listeners and
history writes), value semantics, and subscription capabilities, and the shared-double
conformance check only asserted method *presence* via ``hasattr``.

This suite pins the unified double in ``tests.opcua_fake``:

- structural conformance is proven by a plain ``OpcUaClientProtocol`` assignment, which strict
  mypy verifies against the port signatures (no ``cast`` masking drift);
- responses are validated as bodies: DTO fields, counters, metrics snapshots, subscription
  handles - not just that a method exists.
"""

from __future__ import annotations

from datetime import datetime, timezone

from i3x_server.application.ports.opcua import OpcUaClientProtocol
from i3x_server.domain.ports.opcua import OpcUaNamespaceInfo, OpcUaSubscriptionCapabilities
from i3x_server.infrastructure.subscriptions.service import SubscriptionService
from tests.opcua_fake import FakeOpcUaClient, SubscriptionClientFake

# Static port conformance: strict mypy verifies every protocol member and signature here.
# This replaces the old hasattr-based presence check (review item 16).
_fake_port: OpcUaClientProtocol = FakeOpcUaClient()
_subscription_port: OpcUaClientProtocol = SubscriptionClientFake()


async def test_read_data_values_body_and_counters() -> None:
    client = FakeOpcUaClient()
    results = await client.read_data_values(["ns=2;s=Temperature", "ns=2;s=Missing"])

    assert len(results) == 2
    first, second = results
    assert first.value.value == 43.5  # stored 42.5 + one drift round
    assert first.quality == "Good"
    assert first.status_code == 0x00000000
    assert first.source_timestamp == datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert second.value.value == 2.0  # unknown node seeds at 1.0 and drifts
    assert client.read_data_values_calls == 1
    assert client.read_values_calls == 0


async def test_read_data_values_serves_overrides_verbatim() -> None:
    client = SubscriptionClientFake()
    override = (await FakeOpcUaClient().read_data_values(["ns=2;s=Temperature"]))[0]
    client.data_value_overrides["ns=2;s=Temperature"] = override
    results = await client.read_data_values(["ns=2;s=Temperature", "ns=2;s=Other"])
    assert results[0] is override
    assert results[1].quality == "Good"


async def test_read_values_body_and_drift() -> None:
    client = FakeOpcUaClient()
    assert await client.read_values(["ns=2;s=Temperature"]) == [43.5]
    assert await client.read_values(["ns=2;s=Temperature"]) == [45.5]
    assert client.read_values_calls == 2


async def test_subscription_client_read_semantics_are_index_based() -> None:
    client = SubscriptionClientFake()
    assert await client.read_values(["ns=2;s=Temperature", "ns=2;s=Pressure"]) == [1.0, 2.0]
    assert await client.read_values(["ns=2;s=Temperature", "ns=2;s=Pressure"]) == [2.0, 3.0]
    assert client.read_values_calls == 2

    results = await client.read_data_values(["ns=2;s=Temperature"])
    assert results[0].value.value == 1.0  # index + read_data_values round
    assert results[0].quality == "Good"
    assert client.read_data_values_calls == 1


async def test_write_value_records_variant_type_and_vqt_body() -> None:
    client = FakeOpcUaClient()
    moment = datetime(2026, 5, 6, 7, 8, tzinfo=timezone.utc)
    await client.write_value("ns=2;s=Temperature", 55.0, "Double", "Uncertain", moment)

    assert client.values["ns=2;s=Temperature"] == 55.0
    assert client.last_write_variant_type_by_node_id["ns=2;s=Temperature"] == "Double"
    assert client.last_write_vqt_by_node_id["ns=2;s=Temperature"] == ("Uncertain", moment)
    assert client.snapshot_request_metrics().write_count == 1

    # A verification read answers the written VQT verbatim (mirrors a preserving server).
    read_back = (await client.read_data_values(["ns=2;s=Temperature"]))[0]
    assert read_back.value.value == 55.0
    assert read_back.quality == "Uncertain"
    assert read_back.status_code == 0x40000000
    assert read_back.source_timestamp == moment


async def test_write_value_failure_is_recorded_in_metrics() -> None:
    client = FakeOpcUaClient()
    failure = RuntimeError("server refused")
    client.write_failures["ns=2;s=Temperature"] = failure
    moment = datetime(2026, 5, 6, 7, 8, tzinfo=timezone.utc)
    failures_before = client.snapshot_request_metrics().failed_request_count

    try:
        await client.write_value("ns=2;s=Temperature", 1.0, None, "Good", moment)
    except RuntimeError as exc:
        assert exc is failure
    else:
        raise AssertionError("write_value must propagate the configured failure")
    assert client.snapshot_request_metrics().failed_request_count == failures_before + 1
    assert client.snapshot_request_metrics().write_count == 0


async def test_write_history_value_replaces_and_sorts_records() -> None:
    client = FakeOpcUaClient()
    earlier = datetime(2026, 1, 2, 0, 0, tzinfo=timezone.utc)
    later = datetime(2026, 1, 3, 0, 0, tzinfo=timezone.utc)
    await client.write_history_value("ns=2;s=Sensor", 2.0, "Good", later)
    await client.write_history_value("ns=2;s=Sensor", 1.0, "Good", earlier)
    records = client.history_values["ns=2;s=Sensor"]
    assert [record.source_timestamp for record in records] == [earlier, later]
    assert [record.value.value for record in records] == [1.0, 2.0]

    # Replacing an existing timestamp keeps exactly one record per timestamp.
    await client.write_history_value("ns=2;s=Sensor", 9.9, "Uncertain", earlier)
    records = client.history_values["ns=2;s=Sensor"]
    assert len(records) == 2
    assert records[0].value.value == 9.9
    assert records[0].quality == "Uncertain"
    assert client.snapshot_request_metrics().history_write_count == 3


async def test_write_history_value_rejects_non_numeric_double() -> None:
    client = FakeOpcUaClient()
    moment = datetime(2026, 1, 2, 0, 0, tzinfo=timezone.utc)
    try:
        await client.write_history_value("ns=2;s=Temperature", "not-a-number", "Good", moment)
    except ValueError as exc:
        assert "Double" in str(exc)
    else:
        raise AssertionError("a Double node must reject a non-numeric history write")


async def test_write_history_value_failure_is_recorded_in_metrics() -> None:
    client = FakeOpcUaClient()
    failure = RuntimeError("history refused")
    client.history_write_failures["ns=2;s=Temperature"] = failure
    failures_before = client.snapshot_request_metrics().failed_request_count
    try:
        await client.write_history_value("ns=2;s=Temperature", 1.0, "Good", datetime.now(timezone.utc))
    except RuntimeError as exc:
        assert exc is failure
    else:
        raise AssertionError("write_history_value must propagate the configured failure")
    assert client.snapshot_request_metrics().failed_request_count == failures_before + 1


async def test_read_history_values_body() -> None:
    client = FakeOpcUaClient()
    result = await client.read_history_values(["ns=2;s=Temperature", "ns=2;s=Unknown"], None, None)
    assert set(result) == {"ns=2;s=Temperature", "ns=2;s=Unknown"}
    assert result["ns=2;s=Unknown"] == []
    records = result["ns=2;s=Temperature"]
    assert [record.value.value for record in records] == [40.0, 41.5]
    assert [record.quality for record in records] == ["Good", "Good"]


async def test_server_status_body_and_counter() -> None:
    client = FakeOpcUaClient()
    status = await client.read_server_status_data_value()
    assert status.quality == "Good"
    body = status.value.value
    assert body["State"] == "Running"
    assert body["SecondsTillShutdown"] == 0
    assert body["BuildInfo"]["ProductUri"] == "urn:vendor:server"
    assert body["BuildInfo"]["SoftwareVersion"] == "1.2.3"
    assert client.server_status_reads == 1
    await client.read_server_status_data_value()
    assert client.server_status_reads == 2


async def test_namespace_bodies_and_runtime_counters() -> None:
    client = FakeOpcUaClient()
    infos = await client.get_namespace_infos()
    assert [info.uri for info in infos] == [
        "http://example.com/i3x",
        "http://example.com/custom",
        "http://example.com/runtime",
    ]
    assert infos[0].display_name == "I3X"
    assert await client.get_namespaces() == [info.uri for info in infos]

    snapshot = client.snapshot_runtime_metrics()
    assert snapshot.namespace_reads == 2
    assert snapshot.namespace_info_count_last == 3

    types = await client.get_object_types()
    assert [info.node_id for info in types] == ["ns=1;i=1001", "ns=1;i=1002"]
    machine = types[0]
    assert machine.browse_name == "MachineType"
    assert machine.properties["temperature"] == "i=11"
    assert [member.browse_name for member in machine.members] == ["temperature", "running", "config"]
    assert machine.members[0].data_type == "i=11"
    assert machine.members[0].modelling_rule == "Mandatory"
    extension = machine.members[2].value
    assert extension.TypeId == "ns=1;i=3001"
    assert extension.Body.thresholds.max == 120.5
    # Typed DTO members expose schema_value explicitly (SimpleNamespace doubles fell back to
    # value via getattr); the schema builder needs the extension body there to derive allOf.
    assert machine.members[2].schema_value.TypeId == "ns=1;i=3001"

    snapshot = client.snapshot_runtime_metrics()
    assert snapshot.object_type_reads == 1
    assert snapshot.object_type_count_last == 2


async def test_configured_infos_and_error_injection() -> None:
    infos = [OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="UA")]
    client = FakeOpcUaClient(namespace_infos=infos)
    assert await client.get_namespace_infos() == infos
    assert await client.get_namespaces() == ["http://opcfoundation.org/UA/"]

    failure = RuntimeError("upstream browse failed")
    errored = FakeOpcUaClient(error=failure)
    for call in (errored.get_namespace_infos(), errored.get_object_types()):
        try:
            await call
        except RuntimeError as exc:
            assert exc is failure
        else:
            raise AssertionError("injected error must propagate unchanged")


async def test_access_and_variant_type_bodies() -> None:
    client = FakeOpcUaClient()
    assert await client.read_write_access("ns=2;s=Temperature") == (True, True)
    assert await client.read_write_access("ns=2;s=Other") == (False, False)
    assert await client.read_history_write_access("ns=2;s=Temperature") == (True, True)
    assert await client.read_history_write_access("ns=2;s=Other") == (False, False)
    assert await client.read_variant_type("ns=2;s=Temperature") == "Double"
    assert await client.read_variant_type("ns=2;s=Other") is None
    assert client.history_update_supported() is None


async def test_limits_and_capabilities_bodies() -> None:
    client = FakeOpcUaClient()
    limits = await client.get_operational_limits()
    assert limits.max_nodes_per_browse == 1000
    assert limits.max_nodes_per_read == 1000

    capabilities = await client.get_subscription_capabilities()
    assert capabilities.max_monitored_items_per_call == 1
    assert capabilities.max_monitored_items_per_subscription == 1

    subscription_capabilities = await SubscriptionClientFake().get_subscription_capabilities()
    assert subscription_capabilities == OpcUaSubscriptionCapabilities(
        max_monitored_items_per_call=100,
        max_subscriptions=100,
        max_monitored_items=100,
        max_subscriptions_per_session=100,
        max_monitored_items_per_subscription=100,
    )


async def test_subscription_flow_records_handles_and_bodies() -> None:
    client = FakeOpcUaClient()
    notifications: list[tuple[str, object]] = []

    class _Handler:
        def datachange_notification(self, node_id: str, value: object, data_value: object) -> None:
            notifications.append((node_id, value))

        def event_notification(self, event: object) -> None:
            del event

        def notification_gap(self) -> None:
            pass

    subscription = await client.create_datachange_subscription(1000.0, _Handler())
    assert client.create_subscription_calls == 1
    assert subscription.id == "ua-sub-1"

    handles = await client.subscribe_data_changes(subscription, ["ns=2;s=Temperature", "ns=2;s=Pressure"])
    assert handles == [1, 2]
    assert client.subscribe_calls == 2
    # Handles stay unique across subsequent calls: asyncua assigns monotonically per item.
    assert await client.subscribe_data_changes(subscription, ["ns=2;s=Other"]) == [3]

    await client.unsubscribe_data_changes(subscription, [1])
    assert client.unsubscribed_calls == [(subscription, [1])]
    await client.delete_subscription(subscription)
    assert client.deleted_subscriptions == [subscription]

    # The recorded handler receives the notification payload as the service would deliver it.
    client.handlers[0].datachange_notification("ns=2;s=Temperature", 7.5, None)
    assert notifications == [("ns=2;s=Temperature", 7.5)]


def test_monitored_item_handles_and_reconnect_listeners() -> None:
    client = FakeOpcUaClient()
    subscription = object()
    assert client.get_monitored_item_handles(subscription, ["ns=2;s=Temperature"]) is None
    client.restored_handles[id(subscription)] = {"ns=2;s=Temperature": 501}
    assert client.get_monitored_item_handles(subscription, ["ns=2;s=Temperature"]) == {"ns=2;s=Temperature": 501}

    async def listener() -> None:
        return None

    client.add_reconnect_listener(listener)
    assert client.listeners == [listener]
    client.remove_reconnect_listener(listener)
    assert client.listeners == []
    assert client.removed_listeners == [listener]


def test_runtime_metrics_snapshot_is_a_copy() -> None:
    client = FakeOpcUaClient()
    snapshot = client.snapshot_runtime_metrics()
    snapshot.browse_calls = 99
    assert client.snapshot_runtime_metrics().browse_calls == 0

    client.reset_runtime_metrics()
    assert client.snapshot_runtime_metrics() == client.snapshot_runtime_metrics()


def test_connection_snapshot_body() -> None:
    client = FakeOpcUaClient()
    snapshot = client.get_connection_snapshot()
    assert snapshot.state == "Connected"
    assert snapshot.endpoint == "opc.tcp://opcua.umati.app:4843"
    assert snapshot.since == datetime(2026, 1, 1, 8, 11, 24, tzinfo=timezone.utc)


async def test_call_method_echoes_body() -> None:
    client = FakeOpcUaClient()
    result = await client.call_method("ns=2;s=Machine", "ns=2;s=Reset", [1, 2])
    assert result == {"object": "ns=2;s=Machine", "method": "ns=2;s=Reset", "args": [1, 2]}


def test_subscription_service_satisfies_port_with_the_canonical_double() -> None:
    # No cast: the canonical double structurally satisfies OpcUaClientProtocol, so the
    # service-to-port assignment is verified by strict mypy (replaces the stub + cast).
    service = SubscriptionService(opcua_client=FakeOpcUaClient(), interval_seconds=1.0)
    assert service is not None


async def test_browse_tree_and_type_hierarchy_bodies() -> None:
    client = FakeOpcUaClient()
    assert await client.browse_tree() == []
    assert await client.resolve_type_definition_hierarchy("ns=1;i=1001") == []
    assert await client.resolve_type_definition_hierarchies(["ns=1;i=1001", "ns=1;i=1002"]) == {
        "ns=1;i=1001": [],
        "ns=1;i=1002": [],
    }
