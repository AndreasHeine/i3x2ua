from __future__ import annotations

import asyncio
from typing import cast

import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from i3x_server.application.dependencies import (
    get_model_builder,
    get_opcua_client,
    get_or_build_model,
    get_subscription_service,
)
from i3x_server.application.ports.subscription import SubscriptionServicePort
from i3x_server.application.state import GatewayState, install_gateway_state
from i3x_server.domain.ports.opcua import OpcUaClientProtocol
from i3x_server.model.builder import ModelBuilder
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult


def _request_with_state(state: GatewayState) -> Request:
    app = FastAPI()
    install_gateway_state(app, state)
    return Request({"type": "http", "app": app})


def _builder_stub() -> ModelBuilder:
    """A ``GatewayState`` field expects the concrete builder; the dependency only reads it back."""
    return cast(ModelBuilder, object())


def _client_stub() -> OpcUaClientProtocol:
    """Same stand-in logic for the OPC UA port."""
    return cast(OpcUaClientProtocol, object())


def _service_stub() -> SubscriptionServicePort:
    """Same stand-in logic for the subscription port."""
    return cast(SubscriptionServicePort, object())


def _build_result() -> BuildResult:
    node = ModelNode(
        id="asset-1",
        name="Asset",
        kind="asset",
        type=None,
        children=[],
        source_node_id="ns=2;s=Asset",
    )
    return BuildResult(
        nodes_by_id={node.id: node},
        root_ids=[node.id],
        children_by_id={node.id: []},
        instances_by_type_id={},
        property_to_node={},
        action_to_method={},
    )


def test_dependency_getters_return_initialized_instances() -> None:
    state = GatewayState(
        model_builder=_builder_stub(),
        opcua_client=_client_stub(),
        subscription_service=_service_stub(),
    )
    request = _request_with_state(state)
    assert get_model_builder(request) is state.model_builder
    assert get_opcua_client(request) is state.opcua_client
    assert get_subscription_service(request) is state.subscription_service


def test_dependency_getters_raise_when_unset() -> None:
    # A fresh container has every field declared, so unset collaborators are the
    # declared default (None) and surface as HTTP 500 - not a silent getattr fallback.
    request = _request_with_state(GatewayState())
    with pytest.raises(HTTPException):
        get_model_builder(request)
    with pytest.raises(HTTPException):
        get_opcua_client(request)
    with pytest.raises(HTTPException):
        get_subscription_service(request)


@pytest.mark.asyncio
async def test_get_or_build_model_returns_cached_value() -> None:
    cached = _build_result()
    state = GatewayState(
        model_preload_task=None,
        model_cache=cached,
        model_builder=_builder_stub(),
    )
    request = _request_with_state(state)
    assert await get_or_build_model(request) is cached


@pytest.mark.asyncio
async def test_get_or_build_model_builds_and_sets_cache() -> None:
    built = _build_result()

    class FakeBuilder:
        def __init__(self) -> None:
            self.calls = 0

        async def build(self) -> BuildResult:
            self.calls += 1
            return built

    builder = FakeBuilder()
    state = GatewayState(
        model_preload_task=None,
        model_cache=None,
        # Only ``build`` is implemented by the fake, which is what the lazy path calls.
        model_builder=cast(ModelBuilder, builder),
    )
    request = _request_with_state(state)
    result = await get_or_build_model(request)
    assert result is built
    assert request.app.state.model_cache is built
    assert builder.calls == 1


@pytest.mark.asyncio
async def test_get_or_build_model_coalesces_concurrent_lazy_builds() -> None:
    built = _build_result()
    release = asyncio.Event()

    class FakeBuilder:
        def __init__(self) -> None:
            self.calls = 0

        async def build(self) -> BuildResult:
            self.calls += 1
            await release.wait()
            return built

    builder = FakeBuilder()
    state = GatewayState(model_cache=None, model_builder=cast(ModelBuilder, builder))

    first = asyncio.create_task(get_or_build_model(_request_with_state(state)))
    await asyncio.sleep(0)
    second = asyncio.create_task(get_or_build_model(_request_with_state(state)))
    await asyncio.sleep(0)

    # Both tasks share one app state (the same GatewayState instance).
    assert builder.calls == 1

    release.set()
    results = await asyncio.gather(first, second)
    assert results[0] is built
    assert results[1] is built
    assert state.model_cache is built
    assert builder.calls == 1


@pytest.mark.asyncio
async def test_get_or_build_model_waits_for_preload_task() -> None:
    cached = _build_result()

    request = _request_with_state(GatewayState(model_cache=None, model_builder=_builder_stub()))

    async def preload() -> None:
        await asyncio.sleep(0)
        request.app.state.model_cache = cached

    request.app.state.model_preload_task = asyncio.create_task(preload())
    result = await get_or_build_model(request)
    assert result is cached
