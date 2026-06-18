from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from i3x_server.application.services.mcp import McpService


def _request_with_state(state: SimpleNamespace) -> Request:
    app = FastAPI()
    for key, value in vars(state).items():
        setattr(app.state, key, value)
    return Request({"type": "http", "app": app})


class _GoodTool:
    def to_dict(self) -> dict[str, Any]:
        return {
            "name": "good",
            "input_schema": {"type": "object"},
            "path_parameters": ["id"],
            "query_parameters": ["q"],
            "body_required": True,
        }


class _BadToolReturn:
    def to_dict(self) -> str:
        return "not-a-dict"


@pytest.mark.asyncio
async def test_invoke_tool_rejects_unknown_tool() -> None:
    request = _request_with_state(SimpleNamespace(mcp_tools={}))
    service = McpService(request)
    with pytest.raises(HTTPException) as exc_info:
        await service.invoke_tool("missing", {})
    assert exc_info.value.status_code == 400


def test_get_tools_filters_and_transforms_mcp_tools() -> None:
    tools = {
        "good": _GoodTool(),
        "missing-to-dict": object(),
        "bad-return": _BadToolReturn(),
    }
    request = _request_with_state(SimpleNamespace(mcp_tools=tools))
    service = McpService(request)
    result = service.get_tools()
    assert set(result.keys()) == {"good"}
    assert result["good"]["inputSchema"] == {"type": "object"}
    assert result["good"]["pathParameters"] == ["id"]
    assert result["good"]["queryParameters"] == ["q"]
    assert result["good"]["bodyRequired"] is True


def test_get_tools_returns_empty_for_non_mapping() -> None:
    request = _request_with_state(SimpleNamespace(mcp_tools=["not", "a", "mapping"]))
    service = McpService(request)
    assert service.get_tools() == {}


@pytest.mark.asyncio
async def test_invoke_tool_returns_payload_from_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request_with_state(SimpleNamespace(mcp_tools={"tool-1": _GoodTool()}))
    service = McpService(request)

    async def _fake_invoke(request_obj: Request, tool: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        del request_obj, tool, arguments
        return {"ok": True}

    monkeypatch.setattr("i3x_server.mcp.invoke_mcp_tool", _fake_invoke)
    assert await service.invoke_tool("tool-1", {"x": 1}) == {"ok": True}


@pytest.mark.asyncio
async def test_invoke_tool_rejects_non_dict_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request_with_state(SimpleNamespace(mcp_tools={"tool-1": _GoodTool()}))
    service = McpService(request)

    async def _fake_invoke(request_obj: Request, tool: Any, arguments: dict[str, Any]) -> list[str]:
        del request_obj, tool, arguments
        return ["invalid"]

    monkeypatch.setattr("i3x_server.mcp.invoke_mcp_tool", _fake_invoke)
    with pytest.raises(HTTPException) as exc_info:
        await service.invoke_tool("tool-1", {})
    assert exc_info.value.status_code == 500


def test_get_prompts_delegates_to_prompt_api(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request_with_state(SimpleNamespace(mcp_tools={}))
    service = McpService(request, prompt_registry=None)

    def _fake_list_prompt_metadata(registry: Any) -> list[dict[str, Any]]:
        del registry
        return [{"name": "p1", "description": "d"}]

    monkeypatch.setattr("i3x_server.prompts.api.list_prompt_metadata", _fake_list_prompt_metadata)
    assert service.get_prompts() == [{"name": "p1", "description": "d"}]


@pytest.mark.asyncio
async def test_get_prompt_delegates_to_prompt_api(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request_with_state(SimpleNamespace(mcp_tools={}))
    service = McpService(request, prompt_registry=None)

    def _fake_get_prompt(registry: Any, name: str) -> dict[str, Any]:
        del registry
        return {"name": name, "messages": []}

    monkeypatch.setattr("i3x_server.prompts.api.get_prompt", _fake_get_prompt)
    assert await service.get_prompt("p1") == {"name": "p1", "messages": []}


@pytest.mark.asyncio
async def test_execute_prompt_delegates_to_prompt_api(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request_with_state(SimpleNamespace(mcp_tools={}))
    service = McpService(request, prompt_registry=None)

    def _fake_execute_prompt(registry: Any, name: str, parameters: dict[str, Any]) -> dict[str, Any]:
        del registry
        return {"name": name, "rendered": parameters}

    monkeypatch.setattr("i3x_server.prompts.api.execute_prompt", _fake_execute_prompt)
    assert await service.execute_prompt("p1", {"k": "v"}) == {"name": "p1", "rendered": {"k": "v"}}


def test_get_resources_includes_builtin_and_prompt_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request_with_state(SimpleNamespace(mcp_tools={}))
    service = McpService(request, prompt_registry=None)

    monkeypatch.setattr(
        service,
        "get_prompts",
        lambda: [{"name": "prompt-a", "description": "Prompt A"}],
    )
    resources = service.get_resources()
    uris = {item["uri"] for item in resources}
    assert "i3x://openapi" in uris
    assert "i3x://mcp-overrides" in uris
    assert "i3x://docs/quick-reference" in uris
    assert "i3x://prompts/prompt-a" in uris


@pytest.mark.asyncio
async def test_read_resource_openapi_returns_json_text(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    monkeypatch.setattr(app, "openapi", lambda: {"openapi": "3.1.0"})
    request = Request({"type": "http", "app": app})
    service = McpService(request)
    payload = await service.read_resource("i3x://openapi")
    assert payload["mimeType"] == "application/json"
    assert json.loads(payload["text"]) == {"openapi": "3.1.0"}


@pytest.mark.asyncio
async def test_read_resource_reads_project_files() -> None:
    request = _request_with_state(SimpleNamespace(mcp_tools={}))
    service = McpService(request)
    overrides = await service.read_resource("i3x://mcp-overrides")
    quickref = await service.read_resource("i3x://docs/quick-reference")
    assert overrides["mimeType"] == "application/json"
    assert quickref["mimeType"] == "text/markdown"
    assert overrides["text"].strip()
    assert quickref["text"].strip()


@pytest.mark.asyncio
async def test_read_resource_prompt_uri_uses_get_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request_with_state(SimpleNamespace(mcp_tools={}))
    service = McpService(request)

    async def _fake_get_prompt(name: str) -> dict[str, Any]:
        return {"name": name, "messages": [{"role": "user", "content": "hello"}]}

    monkeypatch.setattr(service, "get_prompt", _fake_get_prompt)
    payload = await service.read_resource("i3x://prompts/demo")
    assert payload["mimeType"] == "application/json"
    assert json.loads(payload["text"])["name"] == "demo"


@pytest.mark.asyncio
async def test_read_resource_rejects_unknown_uri() -> None:
    request = _request_with_state(SimpleNamespace(mcp_tools={}))
    service = McpService(request)
    with pytest.raises(HTTPException) as exc_info:
        await service.read_resource("i3x://unknown")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_read_resource_returns_404_when_files_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request_with_state(SimpleNamespace(mcp_tools={}))
    service = McpService(request)
    monkeypatch.setattr(service, "_get_project_root", lambda: tmp_path)
    with pytest.raises(HTTPException) as exc_overrides:
        await service.read_resource("i3x://mcp-overrides")
    with pytest.raises(HTTPException) as exc_quickref:
        await service.read_resource("i3x://docs/quick-reference")
    assert exc_overrides.value.status_code == 404
    assert exc_quickref.value.status_code == 404


def test_get_roots_returns_empty_without_model_cache() -> None:
    request = _request_with_state(SimpleNamespace(model_cache=None))
    service = McpService(request)
    assert service.get_roots() == []


def test_get_roots_uses_node_name_or_fallback_id() -> None:
    node = SimpleNamespace(name="Root Name")
    model_cache = SimpleNamespace(root_ids=["root-1", "root-missing"], nodes_by_id={"root-1": node})
    request = _request_with_state(SimpleNamespace(model_cache=model_cache))
    service = McpService(request)
    roots = service.get_roots()
    assert roots == [
        {"uri": "i3x://roots/root-1", "name": "Root Name"},
        {"uri": "i3x://roots/root-missing", "name": "root-missing"},
    ]


def test_get_project_root_points_to_repository_root() -> None:
    request = _request_with_state(SimpleNamespace(mcp_tools={}))
    service = McpService(request)
    root = service._get_project_root()
    assert (root / "pyproject.toml").exists()
