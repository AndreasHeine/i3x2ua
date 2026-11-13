from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from i3x_server.application.dependencies import (
    get_model_query_service,
    get_namespace_snapshot,
    get_object_value_service,
    get_subscription_app_service,
)
from i3x_server.application.state import GatewayState, install_gateway_state
from i3x_server.domain.ports.opcua import OpcUaClientProtocol, OpcUaNamespaceInfo
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult


def _request() -> Request:
    app = FastAPI()
    install_gateway_state(app, GatewayState())
    return Request({"type": "http", "app": app})


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


@pytest.mark.asyncio
async def test_application_dependency_factories() -> None:
    request = _request()
    model = _model()
    opcua_client = SimpleNamespace()
    subscription_service = SimpleNamespace()

    model_query = await get_model_query_service(request=request, model=model, opcua_client=opcua_client)
    assert model_query.model is model
    assert model_query._namespace_cache is request.app.state.namespace_query_cache

    value_service = await get_object_value_service(request=request, model=model, opcua_client=opcua_client)
    assert value_service.model is model
    assert value_service.request is request

    sub_service = await get_subscription_app_service(
        request=request,
        model=model,
        opcua_client=opcua_client,
        subscription_service=subscription_service,
    )
    assert sub_service.model is model
    assert sub_service.request is request


@pytest.mark.asyncio
async def test_model_query_services_share_namespace_snapshot_cache() -> None:
    """Services built per request must reuse the holder's converted namespace view."""
    request = _request()
    model = _model()
    fetches = 0

    class _NamespaceClient:
        def __init__(self) -> None:
            self.infos = [OpcUaNamespaceInfo(uri="urn:one", display_name="One")]

        async def get_namespace_infos(self) -> list[OpcUaNamespaceInfo]:
            nonlocal fetches
            fetches += 1
            return self.infos

    client = _NamespaceClient()
    typed_client = cast(OpcUaClientProtocol, client)
    first = await get_model_query_service(request=request, model=model, opcua_client=typed_client)
    second = await get_model_query_service(request=request, model=model, opcua_client=typed_client)
    assert first._namespace_cache is second._namespace_cache is get_namespace_snapshot(request)

    namespaces = await first.get_namespaces()
    assert [namespace.uri for namespace in namespaces] == ["urn:one"]
    assert await second.get_namespaces() == namespaces

    # A new list object from the client (refresh/reconnect) re-resolves the snapshot.
    client.infos = [OpcUaNamespaceInfo(uri="urn:two", display_name="Two")]
    refreshed = await second.get_namespaces()
    assert [namespace.uri for namespace in refreshed] == ["urn:two"]
    assert fetches == 3

    infos = await first.get_namespace_infos()
    assert infos is client.infos


def test_main_module_uses_create_app(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_app = object()
    monkeypatch.setattr("i3x_server.bootstrap.app_factory.create_app", lambda: fake_app)
    module = importlib.import_module("i3x_server.main")
    reloaded = importlib.reload(module)
    assert reloaded.app is fake_app
