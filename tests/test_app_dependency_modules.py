from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from i3x_server.api.dependencies import get_model_query_service
from i3x_server.application.dependencies import (
    get_mcp_service,
    get_object_value_service,
    get_subscription_app_service,
)
from i3x_server.prompts.registry import PromptRegistry
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult


def _request() -> Request:
    app = FastAPI()
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
async def test_api_and_application_dependency_factories() -> None:
    request = _request()
    model = _model()
    opcua_client = SimpleNamespace()
    subscription_service = SimpleNamespace()

    model_query = await get_model_query_service(model=model, opcua_client=opcua_client)
    assert model_query.model is model

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


def test_get_mcp_service_registry_type_guard() -> None:
    request = _request()
    request.app.state.mcp_prompts = {"not": "registry"}
    service_without_registry = get_mcp_service(request)
    assert service_without_registry.prompt_registry is None

    registry = PromptRegistry({})
    request.app.state.mcp_prompts = registry
    service_with_registry = get_mcp_service(request)
    assert service_with_registry.prompt_registry is registry


def test_main_module_uses_create_app(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_app = object()
    monkeypatch.setattr("i3x_server.bootstrap.app_factory.create_app", lambda: fake_app)
    module = importlib.import_module("i3x_server.main")
    reloaded = importlib.reload(module)
    assert reloaded.app is fake_app
