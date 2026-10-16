from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from asyncua import ua
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
