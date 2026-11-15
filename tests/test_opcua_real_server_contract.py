"""Real asyncua-server contract tests for the transport-critical adapter paths (L15).

The unit suites drive the fake client, so the zero-hit functions in ``values.py`` and
``metadata.py`` (write access, variant types, method calls, malformed capability
metadata) never executed against an actual OPC UA peer. These tests run the adapter
against an in-process asyncua server and pin the behavior a real server produces -
including where asyncua differs from the gateway's expectations, so those differences
are documented instead of latent.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest
from asyncua import ua
from asyncua.server.server import Server
from asyncua.ua.attribute_ids import AttributeIds
from asyncua.ua.status_codes import StatusCodes
from asyncua.ua.uaerrors import BadNodeIdUnknown, BadUnexpectedError, BadUserAccessDenied

from i3x_server.infrastructure.opcua.client import OpcUaClient


async def _multiply(parent: Any, first: Any, second: Any) -> list[ua.Variant]:
    del parent
    return [ua.Variant(int(first.Value) * int(second.Value), ua.VariantType.Int32)]


async def _explode(parent: Any) -> list[ua.Variant]:
    del parent
    raise RuntimeError("method exploded")


@contextlib.asynccontextmanager
async def _contract_server(unused_tcp_port: int) -> AsyncIterator[tuple[Server, str]]:
    server = Server()
    await server.init()
    endpoint = f"opc.tcp://127.0.0.1:{unused_tcp_port}/freeopcua/server/"
    server.set_endpoint(endpoint)
    ns = ua.Int16(await server.register_namespace("urn:contract"))

    machine = await server.nodes.objects.add_object(ua.NodeId(ua.String("Machine"), ns), "Machine")
    await machine.add_variable(ua.NodeId(ua.String("Temperature"), ns), "Temperature", 42.0)
    setpoint = await machine.add_variable(ua.NodeId(ua.String("Setpoint"), ns), "Setpoint", 1.0)
    await setpoint.set_writable()
    await machine.add_variable(ua.NodeId(ua.String("Blob"), ns), "Blob", ua.Variant(b"abc", ua.VariantType.ByteString))
    samples = await machine.add_variable(
        ua.NodeId(ua.String("Samples"), ns), "Samples", ua.Variant([1.0, 2.0], ua.VariantType.Double)
    )
    await samples.write_attribute(AttributeIds.ValueRank, ua.DataValue(ua.Variant(1, ua.VariantType.Int32)))
    uncertain = await machine.add_variable(ua.NodeId(ua.String("UncertainVar"), ns), "UncertainVar", 7.0)
    await uncertain.write_attribute(
        AttributeIds.Value,
        ua.DataValue(
            ua.Variant(7.0, ua.VariantType.Double),
            StatusCode=ua.StatusCode(ua.UInt32(StatusCodes.UncertainInitialValue)),
        ),
    )
    await machine.add_variable(ua.NodeId(ua.String("ReadOnlyVar"), ns), "ReadOnlyVar", 1.0)
    mixed = await machine.add_variable(ua.NodeId(ua.String("MixedVar"), ns), "MixedVar", 1.0)
    await mixed.write_attribute(AttributeIds.AccessLevel, ua.DataValue(ua.Variant(3, ua.VariantType.Byte)))
    await mixed.write_attribute(AttributeIds.UserAccessLevel, ua.DataValue(ua.Variant(1, ua.VariantType.Byte)))
    await machine.add_method(
        ua.NodeId(ua.String("Multiply"), ns), "Multiply", _multiply, [ua.VariantType.Int32], [ua.VariantType.Int32]
    )
    await machine.add_method(ua.NodeId(ua.String("Explode"), ns), "Explode", _explode, [], [])

    pump = await server.nodes.base_object_type.add_object_type(ua.NodeId(ua.String("PumpType"), ns), "PumpType")
    flow = await pump.add_property(ua.NodeId(ua.String("Flow"), ns), "Flow", 0.0)
    await flow.set_modelling_rule(True)
    await flow.write_attribute(
        AttributeIds.Description,
        ua.DataValue(ua.Variant(ua.LocalizedText("flow rate"), ua.VariantType.LocalizedText)),
    )
    await pump.add_property(
        ua.NodeId(ua.String("MissingMeta"), ns), "MissingMeta", ua.Variant(None, ua.VariantType.Null)
    )

    async with server:
        yield server, endpoint


@contextlib.asynccontextmanager
async def _client(endpoint: str) -> AsyncIterator[OpcUaClient]:
    client = OpcUaClient(endpoint)
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()


async def test_reads_values_browse_names_and_variant_types(unused_tcp_port: int) -> None:
    async with _contract_server(unused_tcp_port) as (_server, endpoint):
        async with _client(endpoint) as client:
            assert await client.read_value("ns=2;s=Temperature") == 42.0
            assert await client.read_browse_name("ns=2;s=Temperature") == "Temperature"
            assert await client.read_variant_type("ns=2;s=Temperature") == "Double"
            assert await client.read_variant_type("ns=2;s=Blob") == "ByteString"

            # A missing node is a transport-level failure, not an absent value: the read
            # propagates the status code and counts a failed request.
            with pytest.raises(BadNodeIdUnknown):
                await client.read_value("ns=2;s=Missing")
            assert client.snapshot_request_metrics().failed_request_count == 1

            # Browse names degrade to None: callers key the model on node ids, not names.
            assert await client.read_browse_name("ns=2;s=Missing") is None
            assert await client.read_variant_type("ns=2;s=Missing") is None


async def test_write_access_matrix_from_access_level_bits(unused_tcp_port: int) -> None:
    async with _contract_server(unused_tcp_port) as (_server, endpoint):
        async with _client(endpoint) as client:
            assert await client.read_write_access("ns=2;s=Temperature") == (False, False)
            assert await client.read_write_access("ns=2;s=Setpoint") == (True, True)
            # AccessLevel carries the write bit while UserAccessLevel does not.
            assert await client.read_write_access("ns=2;s=MixedVar") == (True, False)
            assert await client.read_write_access("ns=2;s=ReadOnlyVar") == (False, False)


async def test_write_round_trip_and_server_enforced_rejection(unused_tcp_port: int) -> None:
    async with _contract_server(unused_tcp_port) as (_server, endpoint):
        async with _client(endpoint) as client:
            await client.write_value("ns=2;s=Setpoint", 9.5, "Double", "Good", datetime.now(timezone.utc))
            assert await client.read_value("ns=2;s=Setpoint") == 9.5
            assert client.snapshot_request_metrics().write_count == 1

            # asyncua enforces writes on the *User* access level, so a variable whose
            # AccessLevel advertises CurrentWrite but whose UserAccessLevel does not is
            # rejected over the wire with BadUserAccessDenied (not BadNotWritable).
            with pytest.raises(BadUserAccessDenied):
                await client.write_value("ns=2;s=MixedVar", 9.5, "Double", "Good", datetime.now(timezone.utc))
            with pytest.raises(BadUserAccessDenied):
                await client.write_value("ns=2;s=ReadOnlyVar", 5.0, "Double", "Good", datetime.now(timezone.utc))
            assert client.snapshot_request_metrics().failed_request_count == 2


async def test_data_values_preserve_arrays_bytestrings_and_uncertain_quality(unused_tcp_port: int) -> None:
    async with _contract_server(unused_tcp_port) as (_server, endpoint):
        async with _client(endpoint) as client:
            values = await client.read_data_values(["ns=2;s=Samples", "ns=2;s=Blob", "ns=2;s=UncertainVar"])
            samples, blob, uncertain = values
            assert samples.value is not None
            assert samples.value.value == [1.0, 2.0]
            assert samples.value.variant_type == "Double"
            assert samples.value.is_array is True
            assert blob.value is not None
            assert blob.value.value == b"abc"
            assert blob.value.variant_type == "ByteString"
            assert blob.value.is_array is False
            assert uncertain.quality == "Uncertain"

            # Uncertain quality still counts as a delivered value (PR-09 semantics), and
            # a batched read records one success per delivered node.
            metrics = client.snapshot_request_metrics()
            assert metrics.read_count == 3
            assert metrics.failed_request_count == 0


async def test_method_call_by_node_id_string_returns_server_result(unused_tcp_port: int) -> None:
    async with _contract_server(unused_tcp_port) as (_server, endpoint):
        async with _client(endpoint) as client:
            # The port passes method ids as node-id strings. asyncua's Node.call_method
            # resolves strings as relative browse paths, so the adapter must hand the
            # server a NodeId, not the raw string.
            result = await client.call_method("ns=2;s=Machine", "ns=2;s=Multiply", [3, 4])
            assert result == 12
            metrics = client.snapshot_request_metrics()
            assert metrics.method_call_count == 1
            assert metrics.failed_request_count == 0
            assert client.snapshot_runtime_metrics().method_calls == 1


async def test_method_failures_are_recorded_without_success(unused_tcp_port: int) -> None:
    async with _contract_server(unused_tcp_port) as (_server, endpoint):
        async with _client(endpoint) as client:
            # A server-side callback exception surfaces as BadUnexpectedError and counts
            # as a failed request; the method success counter must not move.
            with pytest.raises(BadUnexpectedError):
                await client.call_method("ns=2;s=Machine", "ns=2;s=Explode", [])
            metrics = client.snapshot_request_metrics()
            assert metrics.method_call_count == 0
            assert metrics.failed_request_count == 1
            assert client.snapshot_runtime_metrics().method_calls == 1

            # asyncua does not enforce UserExecutable, so an adapter-side permission
            # check is the only gate - pinned here as a real-server contract.
            assert await client.call_method("ns=2;s=Machine", "ns=2;s=Multiply", [3, 4]) == 12


async def test_server_status_reads_extension_object_with_good_quality(unused_tcp_port: int) -> None:
    async with _contract_server(unused_tcp_port) as (_server, endpoint):
        async with _client(endpoint) as client:
            status = await client.read_server_status_data_value()
            assert status.quality == "Good"
            assert status.status_code == 0
            assert status.value is not None
            assert status.value.variant_type == "ExtensionObject"
            assert type(status.value.value).__name__ == "ServerStatusDataType"
            assert status.value.value.State.name == "Running"


async def test_operational_limits_survive_malformed_capability_values(unused_tcp_port: int) -> None:
    async with _contract_server(unused_tcp_port) as (server, endpoint):
        # A capability child whose value is Null must not become a limit.
        caps = server.iserver.aspace[ua.NodeId(ua.Int32(11704))]
        for ref in caps.references:
            child = server.iserver.aspace[ref.NodeId]
            browse_value = child.attributes[AttributeIds.BrowseName].value
            if browse_value is None or browse_value.Value is None:
                continue
            qualified_name = browse_value.Value.Value
            if isinstance(qualified_name, ua.QualifiedName) and qualified_name.Name == "MaxNodesPerRead":
                child.attributes[AttributeIds.Value].value = ua.DataValue(ua.Variant(None, ua.VariantType.Null))

        async with _client(endpoint) as client:
            limits = await client.get_operational_limits()
            assert limits.max_nodes_per_read is None
            assert limits.max_nodes_per_browse is not None
            assert limits.max_nodes_per_history_read_data is not None

        # A server without a capabilities node falls back to internal defaults.
        del server.iserver.aspace[ua.NodeId(ua.Int32(11704))]
        async with _client(endpoint) as client:
            limits = await client.get_operational_limits()
            assert (
                limits.max_nodes_per_browse
                is limits.max_nodes_per_read
                is limits.max_nodes_per_history_read_data
                is None
            )


async def test_subscription_capabilities_fall_back_per_node_when_batch_fails(unused_tcp_port: int) -> None:
    async with _contract_server(unused_tcp_port) as (server, endpoint):
        # Removing one capability node makes the batched read fail; the fallback must
        # still resolve the capabilities that do exist.
        del server.iserver.aspace[ua.NodeId(ua.Int32(24096))]
        async with _client(endpoint) as client:
            capabilities = await client.get_subscription_capabilities()
            assert capabilities.max_monitored_items_per_call == 10000
            assert capabilities.max_subscriptions is None
            assert capabilities.max_monitored_items is None
            assert capabilities.max_subscriptions_per_session is None
            assert capabilities.max_monitored_items_per_subscription is None


async def test_object_type_members_read_from_real_server_metadata(unused_tcp_port: int) -> None:
    async with _contract_server(unused_tcp_port) as (_server, endpoint):
        async with _client(endpoint) as client:
            pump = next(info for info in await client.get_object_types() if info.node_id == "ns=2;s=PumpType")
            assert pump.parent_node_id == "i=58"
            assert pump.is_abstract is False

            flow = next(member for member in pump.members if member.browse_name == "Flow")
            assert flow.description == "flow rate"
            assert flow.modelling_rule == "Mandatory"
            assert flow.node_class == "Variable"
            assert flow.data_type == "i=11"
            assert flow.variant_type == "Double"
            assert flow.value == 0.0
            assert flow.value_rank == -1

            # A member without a modelling rule or a value keeps both absent instead of
            # failing the whole type read.
            missing = next(member for member in pump.members if member.browse_name == "MissingMeta")
            assert missing.modelling_rule is None
            assert missing.value is None
            assert missing.variant_type == "Null"
            assert missing.data_type == "i=0"
