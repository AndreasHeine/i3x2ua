from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from i3x_server.application.errors import ApplicationServiceError
from i3x_server.application.services.model_query import (
    ModelQueryService,
    Namespace,
    ServerCapabilities,
    ServerInfo,
    _build_server_info,
    _to_namespace,
)
from i3x_server.domain.ports.opcua import OpcUaClientProtocol, OpcUaNamespaceInfo, OpcUaObjectTypeInfo
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult


def _model() -> BuildResult:
    node = ModelNode(
        id="asset-root",
        name="Root",
        kind="asset",
        type=None,
        children=[],
        source_node_id="ns=2;s=Root",
    )
    return BuildResult(
        nodes_by_id={node.id: node},
        root_ids=[node.id],
        children_by_id={node.id: []},
        instances_by_type_id={},
        property_to_node={},
        action_to_method={},
    )


class _FakeOpcUaClient:
    def __init__(self, infos: list[OpcUaNamespaceInfo], error: Exception | None = None) -> None:
        self._infos = infos
        self._error = error
        self.calls = 0
        self.object_types: list[OpcUaObjectTypeInfo] = []

    async def get_namespace_infos(self) -> list[OpcUaNamespaceInfo]:
        self.calls += 1
        if self._error:
            raise self._error
        return self._infos

    async def get_object_types(self) -> list[OpcUaObjectTypeInfo]:
        if self._error:
            raise self._error
        return self.object_types


def test_namespace_model_dump() -> None:
    ns = Namespace(uri="http://example.com", displayName="Example")
    assert ns.model_dump() == {"uri": "http://example.com", "displayName": "Example"}


def test_server_capabilities_model_dump() -> None:
    caps = ServerCapabilities(query={"history": True}, update={"current": False}, subscribe={"stream": True})
    assert caps.model_dump() == {
        "query": {"history": True},
        "update": {"current": False},
        "subscribe": {"stream": True},
    }


def test_server_info_model_dump() -> None:
    info = ServerInfo(
        specVersion="1.0",
        serverVersion="2.0.0",
        serverName="Gateway",
        capabilities=ServerCapabilities(query={}, update={}, subscribe={}),
    )
    assert info.model_dump() == {
        "specVersion": "1.0",
        "serverVersion": "2.0.0",
        "serverName": "Gateway",
        "capabilities": {"query": {}, "update": {}, "subscribe": {}},
    }


def test_to_namespace_prefers_display_name_or_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    with_display = OpcUaNamespaceInfo(uri="http://example.com/with", display_name="With")
    assert _to_namespace(with_display).model_dump() == {"uri": "http://example.com/with", "displayName": "With"}

    monkeypatch.setattr("i3x_server.application.services.model_query.display_name_for_uri", lambda uri: f"Name:{uri}")
    without_display = OpcUaNamespaceInfo(uri="http://example.com/without", display_name="")
    assert _to_namespace(without_display).model_dump() == {
        "uri": "http://example.com/without",
        "displayName": "Name:http://example.com/without",
    }


def test_build_server_info_allows_explicit_values() -> None:
    info = _build_server_info(server_version="9.9.9", server_name="Custom")
    dumped = info.model_dump()
    assert dumped["specVersion"] == "1.0"
    assert dumped["serverVersion"] == "9.9.9"
    assert dumped["serverName"] == "Custom"
    assert dumped["capabilities"]["query"]["history"] is True


@pytest.mark.asyncio
async def test_get_server_info_returns_server_info() -> None:
    service = ModelQueryService(SimpleNamespace(), _model())
    info = await service.get_server_info()
    assert isinstance(info, ServerInfo)
    assert info.specVersion == "1.0"


@pytest.mark.asyncio
async def test_get_namespaces_uses_cache() -> None:
    infos = [
        OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="UA"),
        OpcUaNamespaceInfo(uri="http://example.com/custom", display_name="Custom"),
    ]
    client = _FakeOpcUaClient(infos)
    service = ModelQueryService(cast(OpcUaClientProtocol, client), _model())

    first = await service.get_namespaces()
    second = await service.get_namespaces()

    assert [item.model_dump() for item in first] == [
        {"uri": "http://opcfoundation.org/UA/", "displayName": "UA"},
        {"uri": "http://example.com/custom", "displayName": "Custom"},
    ]
    assert [item.model_dump() for item in second] == [item.model_dump() for item in first]
    assert client.calls == 1


@pytest.mark.asyncio
async def test_get_namespaces_wraps_client_errors() -> None:
    client = _FakeOpcUaClient([], error=RuntimeError("namespace read failed"))
    service = ModelQueryService(cast(OpcUaClientProtocol, client), _model())
    with pytest.raises(ApplicationServiceError) as exc_info:
        await service.get_namespaces()
    assert exc_info.value.status_code == 502
    assert exc_info.value.error_code == "OpcUaNamespaceError"


@pytest.mark.asyncio
async def test_get_namespace_infos_uses_cache_and_empty_default() -> None:
    client = _FakeOpcUaClient([])
    service = ModelQueryService(cast(OpcUaClientProtocol, client), _model())
    first = await service.get_namespace_infos()
    second = await service.get_namespace_infos()
    assert first == []
    assert second == []
    assert client.calls == 1


@pytest.mark.asyncio
async def test_get_object_types_returns_items_and_honors_namespace_filter() -> None:
    infos = [
        OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="UA"),
        OpcUaNamespaceInfo(uri="http://example.com/custom", display_name="Custom"),
    ]
    client = _FakeOpcUaClient(infos)
    client.object_types = [
        OpcUaObjectTypeInfo(
            node_id="ns=1;i=1001",
            parent_node_id=None,
            browse_name="MachineType",
            display_name="Machine Type",
            properties={},
        )
    ]
    service = ModelQueryService(cast(OpcUaClientProtocol, client), _model())

    all_items = await service.get_object_types()
    assert len(all_items) == 1
    assert all_items[0]["displayName"] == "Machine Type"
    assert all_items[0]["namespaceUri"] == "http://example.com/custom"
    assert isinstance(all_items[0]["elementId"], str)

    filtered = await service.get_object_types(namespace_uri="http://example.com/custom")
    assert len(filtered) == 1
    empty = await service.get_object_types(namespace_uri="http://example.com/other")
    assert empty == []


@pytest.mark.asyncio
async def test_get_object_types_wraps_client_errors() -> None:
    client = _FakeOpcUaClient([], error=RuntimeError("object type read failed"))
    service = ModelQueryService(cast(OpcUaClientProtocol, client), _model())
    with pytest.raises(ApplicationServiceError) as exc_info:
        await service.get_object_types()
    assert exc_info.value.status_code == 502
    assert exc_info.value.error_code == "OpcUaObjectTypeError"


@pytest.mark.asyncio
async def test_get_relationship_types_contains_defaults_and_model_graph_items() -> None:
    model = _model()
    model.graph_relationship_names = {"ConnectedTo"}
    service = ModelQueryService(cast(OpcUaClientProtocol, _FakeOpcUaClient([])), model)

    items = await service.get_relationship_types()
    element_ids = {item["elementId"] for item in items}
    assert "HasParent" in element_ids
    assert "ConnectedTo" in element_ids
    assert "inverseOf_ConnectedTo" in element_ids

    filtered = await service.get_relationship_types(namespace_uri="https://cesmii.org/i3x")
    assert all(item["namespaceUri"] == "https://cesmii.org/i3x" for item in filtered)


@pytest.mark.asyncio
async def test_get_objects_returns_model_nodes_and_filtering() -> None:
    service = ModelQueryService(cast(OpcUaClientProtocol, _FakeOpcUaClient([])), _model())

    all_objects = await service.get_objects()
    assert len(all_objects) == 1
    assert all_objects[0]["elementId"] == "asset-root"
    assert all_objects[0]["metadata"] is None

    filtered = await service.get_objects(element_ids=["asset-root"], include_metadata=True)
    assert len(filtered) == 1
    assert isinstance(filtered[0]["metadata"], dict)

    missing = await service.get_objects(element_ids=["missing"])
    assert missing == []
