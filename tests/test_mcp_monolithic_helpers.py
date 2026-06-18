from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from i3x_server.api.mcp.monolithic import (
    _handle_jsonrpc,
    _jsonrpc_error,
    _jsonrpc_notification,
    _jsonrpc_response,
    _prompt_registry,
    _read_resource_content,
    _resource_catalog,
    _roots_catalog,
    _tool_catalog,
)
from i3x_server.prompts.registry import PromptDefinition, PromptRegistry


def _request(state: dict[str, Any] | None = None) -> Request:
    app = FastAPI()
    app.openapi = lambda: {"openapi": "3.1.0"}
    for key, value in (state or {}).items():
        setattr(app.state, key, value)
    return Request({"type": "http", "app": app, "headers": []})


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"Tool {name}"
        self.input_schema = {"type": "object"}

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


def test_jsonrpc_payload_helpers() -> None:
    assert _jsonrpc_response(1, {"ok": True}) == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    assert _jsonrpc_error(1, -1, "bad") == {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "bad"}}
    assert _jsonrpc_notification("notifications/test") == {"jsonrpc": "2.0", "method": "notifications/test"}


def test_tool_and_resource_and_roots_catalogs(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request({"mcp_tools": {"a": _Tool("a")}})
    tools = _tool_catalog(request)
    assert tools == [{"name": "a", "description": "Tool a", "inputSchema": {"type": "object"}}]

    request_bad_tools = _request({"mcp_tools": []})
    assert _tool_catalog(request_bad_tools) == []

    monkeypatch.setattr(
        "i3x_server.api.mcp.monolithic.list_prompt_metadata",
        lambda registry: [{"name": "p1", "description": "Prompt one"}] if registry else [],
    )
    prompt_registry = PromptRegistry(
        {
            "p1": PromptDefinition(
                name="p1",
                description="Prompt one",
                inputs=("x",),
                template="{{x}}",
            )
        }
    )
    request_with_prompts = _request({"mcp_prompts": prompt_registry})
    resources = _resource_catalog(request_with_prompts)
    assert any(item["uri"] == "i3x://prompts/p1" for item in resources)

    request_no_model = _request()
    assert _roots_catalog(request_no_model) == []

    model_cache = SimpleNamespace(
        root_ids=["root-1", "root-missing"],
        nodes_by_id={"root-1": SimpleNamespace(name="Root 1")},
    )
    request_roots = _request({"model_cache": model_cache})
    assert _roots_catalog(request_roots) == [
        {"uri": "i3x://roots/root-1", "name": "Root 1"},
        {"uri": "i3x://roots/root-missing", "name": "root-missing"},
    ]


def test_prompt_registry_type_guard() -> None:
    request = _request({"mcp_prompts": {"invalid": True}})
    assert _prompt_registry(request) is None


def test_read_resource_content_variants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    overrides_dir = tmp_path / "overrides"
    docs_dir = tmp_path / "docs"
    overrides_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)
    (overrides_dir / "mcp_overrides.json").write_text('{"tools":{}}', encoding="utf-8")
    (docs_dir / "QUICK_REFERENCE.md").write_text("# Quick", encoding="utf-8")

    request = _request()
    monkeypatch.setattr("i3x_server.api.mcp.monolithic._project_root", lambda: tmp_path)
    monkeypatch.setattr("i3x_server.api.mcp.monolithic.get_prompt", lambda registry, name: {"name": name})

    openapi = _read_resource_content(request, "i3x://openapi")
    assert json.loads(openapi["text"]) == {"openapi": "3.1.0"}
    assert _read_resource_content(request, "i3x://mcp-overrides")["mimeType"] == "application/json"
    assert _read_resource_content(request, "i3x://docs/quick-reference")["mimeType"] == "text/markdown"
    assert json.loads(_read_resource_content(request, "i3x://prompts/demo")["text"])["name"] == "demo"

    with pytest.raises(HTTPException):
        _read_resource_content(request, "i3x://unknown")


@pytest.mark.asyncio
async def test_handle_jsonrpc_core_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request({"mcp_tools": {"tool-a": _Tool("tool-a")}})

    assert await _handle_jsonrpc(request, {"jsonrpc": "1.0", "id": 1, "method": "x"}) == _jsonrpc_error(
        1, -32600, "Invalid Request"
    )
    assert await _handle_jsonrpc(request, {"jsonrpc": "2.0", "id": 1, "method": 1}) == _jsonrpc_error(
        1, -32600, "Invalid Request"
    )
    assert await _handle_jsonrpc(request, {"jsonrpc": "2.0", "method": "notifications/ping"}) is None
    assert await _handle_jsonrpc(request, {"jsonrpc": "2.0", "method": "tools/list"}) is None

    initialized = await _handle_jsonrpc(
        request,
        {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "v"}},
    )
    assert initialized is not None
    assert initialized["result"]["protocolVersion"] == "v"

    assert await _handle_jsonrpc(request, {"jsonrpc": "2.0", "id": 3, "method": "notifications/initialized"}) is None
    assert await _handle_jsonrpc(request, {"jsonrpc": "2.0", "id": 4, "method": "tools/list"}) == _jsonrpc_response(
        4, {"tools": _tool_catalog(request)}
    )

    invalid_params = await _handle_jsonrpc(request, {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": []})
    assert invalid_params == _jsonrpc_error(5, -32602, "Invalid params")

    unknown_tool = await _handle_jsonrpc(
        request,
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "missing", "arguments": {}}},
    )
    assert unknown_tool == _jsonrpc_error(6, -32602, "Unknown tool missing")

    async def _invoke_ok(req: Request, tool: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        del req, tool, arguments
        return {"status_code": 200, "body": {"ok": True}}

    monkeypatch.setattr("i3x_server.api.mcp.monolithic.invoke_mcp_tool", _invoke_ok)
    tool_ok = await _handle_jsonrpc(
        request,
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "tool-a", "arguments": {}}},
    )
    assert tool_ok is not None
    assert tool_ok["result"]["content"][0]["type"] == "text"

    async def _invoke_bad(req: Request, tool: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        del req, tool, arguments
        return {"status_code": 500, "body": {"error": "bad"}}

    monkeypatch.setattr("i3x_server.api.mcp.monolithic.invoke_mcp_tool", _invoke_bad)
    tool_bad = await _handle_jsonrpc(
        request,
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "tool-a", "arguments": {}}},
    )
    assert tool_bad == _jsonrpc_error(8, 500, json.dumps({"error": "bad"}, ensure_ascii=False))

    unknown_method = await _handle_jsonrpc(request, {"jsonrpc": "2.0", "id": 9, "method": "x/unknown"})
    assert unknown_method == _jsonrpc_error(9, -32601, "Method not found: x/unknown")
