from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from i3x_server.dependencies import (
    get_model_builder,
    get_opcua_client,
    get_or_build_model,
    get_subscription_service,
)
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult


def _request_with_state(state: SimpleNamespace) -> Request:
    app = FastAPI()
    for key, value in vars(state).items():
        setattr(app.state, key, value)
    return Request({"type": "http", "app": app})


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
        property_to_node={},
        action_to_method={},
    )


def test_dependency_getters_return_initialized_instances() -> None:
    state = SimpleNamespace(model_builder=object(), opcua_client=object(), subscription_service=object())
    request = _request_with_state(state)
    assert get_model_builder(request) is state.model_builder
    assert get_opcua_client(request) is state.opcua_client
    assert get_subscription_service(request) is state.subscription_service


def test_dependency_getters_raise_when_missing() -> None:
    request = _request_with_state(SimpleNamespace())
    with pytest.raises(HTTPException):
        get_model_builder(request)
    with pytest.raises(HTTPException):
        get_opcua_client(request)
    with pytest.raises(HTTPException):
        get_subscription_service(request)


@pytest.mark.asyncio
async def test_get_or_build_model_returns_cached_value() -> None:
    cached = _build_result()
    state = SimpleNamespace(
        model_preload_task=None,
        model_cache=cached,
        model_lock=asyncio.Lock(),
        model_builder=object(),
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
    state = SimpleNamespace(
        model_preload_task=None,
        model_cache=None,
        model_lock=asyncio.Lock(),
        model_builder=builder,
    )
    request = _request_with_state(state)
    result = await get_or_build_model(request)
    assert result is built
    assert request.app.state.model_cache is built
    assert builder.calls == 1


@pytest.mark.asyncio
async def test_get_or_build_model_waits_for_preload_task() -> None:
    cached = _build_result()

    state = SimpleNamespace(
        model_preload_task=None,
        model_cache=None,
        model_lock=asyncio.Lock(),
        model_builder=object(),
    )
    request = _request_with_state(state)

    async def preload() -> None:
        await asyncio.sleep(0)
        request.app.state.model_cache = cached

    request.app.state.model_preload_task = asyncio.create_task(preload())
    result = await get_or_build_model(request)
    assert result is cached
