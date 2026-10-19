from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from asyncua import ua
from asyncua.client.ua_client import UaClientState
from asyncua.common.node import Node
from asyncua.server.server import Server
from asyncua.ua.object_ids import ObjectIds
from asyncua.ua.uaerrors import BadUserAccessDenied
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from i3x_server.api.v1.object_routes import router
from i3x_server.config.settings import Settings
from i3x_server.infrastructure.opcua.client import OpcUaClient
from i3x_server.model.builder import ModelBuilder


@pytest_asyncio.fixture
async def browse_server(unused_tcp_port: int) -> AsyncIterator[tuple[Server, str]]:
    server = Server()
    await server.init()
    endpoint = f"opc.tcp://127.0.0.1:{unused_tcp_port}"
    server.set_endpoint(endpoint)
    namespace = ua.Int16(await server.register_namespace("urn:browse-roots"))
    first = await server.nodes.objects.add_object(ua.NodeId(ua.String("Machine1"), namespace), "Machine1")
    await first.add_variable(ua.NodeId(ua.String("Temperature"), namespace), "Temperature", 42.0)
    await server.nodes.objects.add_object(ua.NodeId(ua.String("Machine2"), namespace), "Machine2")
    async with server:
        yield server, endpoint


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["single", "disjoint", "overlap"])
async def test_browse_roots_model_and_api(browse_server: tuple[Server, str], mode: str) -> None:
    server, endpoint = browse_server
    namespace = ua.Int16(await server.get_namespace_index("urn:browse-roots"))
    first = server.get_node(ua.NodeId(ua.String("Machine1"), namespace))
    second = server.get_node(ua.NodeId(ua.String("Machine2"), namespace))
    if mode == "overlap":
        await first.add_reference(second, ObjectIds.Organizes)
    # A shared descendant also exercises cycle handling without changing the object roots.
    temperature = server.get_node(ua.NodeId(ua.String("Temperature"), namespace))
    await second.add_reference(temperature, ObjectIds.HasComponent)
    pressure = await temperature.add_variable(ua.NodeId(ua.String("Pressure"), namespace), "Pressure", 1.0)
    await pressure.add_reference(temperature, ObjectIds.Organizes)
    settings = Settings(
        opcua_browse_roots=[
            "nsu=urn:browse-roots;s=Machine1",
            "nsu=urn:browse-roots;s=Machine1" if mode == "single" else "nsu=urn:browse-roots;s=Machine2",
            "nsu=urn:browse-roots;s=Machine1",
        ]
    )
    client = OpcUaClient(endpoint, browse_roots=settings.opcua_browse_roots)
    async with client._client:
        builder = ModelBuilder(client)
        model = await builder.build()
        expected_nodes = {"Machine1", "Temperature", "Pressure"}
        if mode != "single":
            expected_nodes.add("Machine2")
        assert len(model.nodes_by_id) == len(expected_nodes)
        assert {node.name for node in model.nodes_by_id.values()} == expected_nodes
        app = FastAPI()
        app.include_router(router)
        app.state.opcua_client = client
        app.state.model_cache = model
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
            response = await api.get("/v1/objects", params={"root": "true"})
        assert response.status_code == 200
        expected = {"Machine1", "Machine2"} if mode == "disjoint" else {"Machine1"}
        assert {item["displayName"] for item in response.json()["result"]} == expected


@pytest.mark.asyncio
async def test_default_browse_root_is_objects() -> None:
    client = OpcUaClient("opc.tcp://localhost:4840")
    roots = await client._resolve_browse_roots()
    assert [root.nodeid for root in roots] == [ua.NodeId(ua.Int32(ObjectIds.ObjectsFolder))]


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 16])
async def test_reconnect_restores_reads_and_native_subscription(
    browse_server: tuple[Server, str], concurrency: int
) -> None:
    server, endpoint = browse_server
    namespace = ua.Int16(await server.get_namespace_index("urn:browse-roots"))
    temperature = server.get_node(ua.NodeId(ua.String("Temperature"), namespace))
    initial_value = asyncio.Event()
    changed_value = asyncio.Event()
    reconnected = asyncio.Event()

    class Handler:
        def datachange_notification(self, node: Node, value: Any, data: Any) -> None:
            if value == 42.0:
                initial_value.set()
            if value == 43.0:
                changed_value.set()

    async def on_reconnect() -> None:
        reconnected.set()

    client = OpcUaClient(
        endpoint,
        browse_concurrency=concurrency,
        connection_monitor_interval_seconds=1,
        reconnect_request_timeout_seconds=2,
    )
    client.add_reconnect_listener(on_reconnect)
    await client.connect()
    reads: list[asyncio.Task[list[ua.DataValue]]] = []
    try:
        subscription = await client.create_datachange_subscription(50, Handler())
        await client.subscribe_data_changes(subscription, [temperature.nodeid.to_string()])
        await asyncio.wait_for(initial_value.wait(), timeout=5)
        transport = client._client.uaclient
        protocol = transport.protocol
        assert protocol is not None
        assert protocol.transport is not None
        async with transport.subscribe_state() as states:
            protocol.transport.abort()
            await states.wait_for_state(UaClientState.RECONNECTING, timeout=5)
            reads = [asyncio.create_task(client.read_data_values([temperature.nodeid.to_string()])) for _ in range(20)]
            await states.wait_for_state(UaClientState.CONNECTED, timeout=5)
        results = await asyncio.wait_for(asyncio.gather(*reads), timeout=5)
        assert all(result[0].Value is not None and result[0].Value.Value == 42.0 for result in results)
        await asyncio.wait_for(reconnected.wait(), timeout=5)
        assert client.get_connection_snapshot().state == "Connected"
        await temperature.write_value(43.0)
        await asyncio.wait_for(changed_value.wait(), timeout=5)
    finally:
        for task in reads:
            task.cancel()
        await asyncio.gather(*reads, return_exceptions=True)
        await client.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identifier",
    ["i=1234", "s=Machine;part=1,2", "g=12345678-1234-1234-1234-123456789abc", "b=AQID"],
)
async def test_browse_root_resolution_preserves_type_and_refreshes_namespace(
    monkeypatch: pytest.MonkeyPatch, identifier: str
) -> None:
    client = OpcUaClient("opc.tcp://localhost:4840", browse_roots=[f"nsu=urn:plant;{identifier}"])
    namespaces = AsyncMock(
        side_effect=[
            ["http://opcfoundation.org/UA/", "urn:plant"],
            ["http://opcfoundation.org/UA/", "urn:other", "urn:plant"],
        ]
    )
    monkeypatch.setattr(client._client, "get_namespace_array", namespaces)
    monkeypatch.setattr(Node, "read_node_class", AsyncMock(return_value=ua.NodeClass.Object))
    parsed = ua.NodeId.from_string(f"nsu=urn:plant;{identifier}")
    for index in (1, 2):
        roots = await client._resolve_browse_roots()
        assert len(roots) == 1
        assert type(roots[0].nodeid) is ua.NodeId
        assert roots[0].nodeid.NamespaceIndex == index
        assert roots[0].nodeid.Identifier == parsed.Identifier
        assert roots[0].nodeid.NodeIdType == parsed.NodeIdType
    assert namespaces.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("root", "error"),
    [
        ("nsu=urn:missing;s=Machine1", "namespace is not available"),
        ("nsu=urn:browse-roots;s=Missing", "Cannot read configured"),
        ("nsu=urn:browse-roots;s=Temperature", "not an Object"),
    ],
)
async def test_invalid_root_fails_entire_build(browse_server: tuple[Server, str], root: str, error: str) -> None:
    _, endpoint = browse_server
    client = OpcUaClient(endpoint, browse_roots=["nsu=urn:browse-roots;s=Machine1", root])
    async with client._client:
        with pytest.raises(ValueError, match=error):
            await ModelBuilder(client).build()
    assert client.snapshot_runtime_metrics().browse_tree_nodes_last == 0


@pytest.mark.asyncio
async def test_unreadable_root_is_not_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpcUaClient("opc.tcp://localhost:4840", browse_roots=["nsu=urn:plant;i=1"])
    monkeypatch.setattr(client._client, "get_namespace_array", AsyncMock(return_value=["urn:plant"]))
    monkeypatch.setattr(
        Node,
        "read_node_class",
        AsyncMock(side_effect=BadUserAccessDenied()),  # type: ignore[no-untyped-call]
    )
    with pytest.raises(ValueError, match="BadUserAccessDenied"):
        await client._resolve_browse_roots()
