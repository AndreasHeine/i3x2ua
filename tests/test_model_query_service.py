from __future__ import annotations

from types import SimpleNamespace

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
from i3x_server.opcua.contracts import OpcUaNamespaceInfo
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

    async def get_namespace_infos(self) -> list[OpcUaNamespaceInfo]:
        self.calls += 1
        if self._error:
            raise self._error
        return self._infos


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
    service = ModelQueryService(client, _model())

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
    service = ModelQueryService(client, _model())
    with pytest.raises(ApplicationServiceError) as exc_info:
        await service.get_namespaces()
    assert exc_info.value.status_code == 502
    assert exc_info.value.error_code == "OpcUaNamespaceError"


@pytest.mark.asyncio
async def test_get_namespace_infos_uses_cache_and_empty_default() -> None:
    client = _FakeOpcUaClient([])
    service = ModelQueryService(client, _model())
    first = await service.get_namespace_infos()
    second = await service.get_namespace_infos()
    assert first == []
    assert second == []
    assert client.calls == 1


@pytest.mark.asyncio
async def test_unimplemented_methods_raise_not_implemented() -> None:
    service = ModelQueryService(_FakeOpcUaClient([]), _model())
    with pytest.raises(NotImplementedError):
        await service.get_object_types()
    with pytest.raises(NotImplementedError):
        await service.get_relationship_types()
    with pytest.raises(NotImplementedError):
        await service.get_objects()
