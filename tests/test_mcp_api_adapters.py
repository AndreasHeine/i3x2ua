from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from i3x_server.api.mcp.jsonrpc import (
    MCP_ROOT_RESOURCE_PREFIX,
    _handle_jsonrpc,
    _jsonrpc_error,
    _jsonrpc_response,
)
from i3x_server.api.mcp.prompts import _prompt_registry
from i3x_server.api.mcp.resources import (
    MCP_IDENTITY_GUIDE,
    MCP_IDENTITY_GUIDE_URI,
    MCP_MAPPING_GUIDE,
    MCP_MAPPING_GUIDE_URI,
    MCP_MODEL_OVERVIEW_URI,
    _read_resource_content,
    _resource_catalog,
)
from i3x_server.api.mcp.roots import _roots_catalog
from i3x_server.api.mcp.tools import _tool_catalog
from i3x_server.application.state import GatewayState, install_gateway_state
from i3x_server.mcp import McpToolDefinition
from i3x_server.prompts.registry import PromptDefinition, PromptRegistry
from i3x_server.schemas.state import BuildResult


def _request(state: dict[str, Any] | None = None) -> Request:
    class _App(FastAPI):
        def openapi(self) -> dict[str, str]:
            return {"openapi": "3.1.0"}

    app = _App()
    install_gateway_state(app, GatewayState(**(state or {})))
    return Request({"type": "http", "app": app, "headers": []})


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"Tool {name}"
        self.input_schema = {"type": "object"}
        definition = McpToolDefinition(name, self.description, "GET", "/test", self.input_schema, (), (), False)
        self.output_schema = definition.output_schema
        self.annotations = definition.annotations

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


def test_jsonrpc_payload_helpers() -> None:
    assert _jsonrpc_response(1, {"ok": True}) == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    assert _jsonrpc_error(1, -1, "bad") == {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "bad"}}


def test_tool_and_resource_and_roots_catalogs(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request({"mcp_tools": {"a": _Tool("a")}})
    tools = _tool_catalog(request)
    assert tools[0]["name"] == "a"
    assert tools[0]["inputSchema"] == {"type": "object"}
    assert tools[0]["outputSchema"]["type"] == "object"
    assert tools[0]["annotations"]["readOnlyHint"] is True

    # The typed container declares ``mcp_tools`` as a mapping; an empty catalog is the
    # negative path now that non-mapping state bags no longer exist.
    request_bad_tools = _request({"mcp_tools": {}})
    assert _tool_catalog(request_bad_tools) == []

    monkeypatch.setattr(
        "i3x_server.api.mcp.resources.list_prompt_metadata",
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
    mapping_guide = next(item for item in resources if item["uri"] == MCP_MAPPING_GUIDE_URI)
    assert mapping_guide["mimeType"] == "text/markdown"
    assert "Composition" in mapping_guide["description"]
    identity_guide = next(item for item in resources if item["uri"] == MCP_IDENTITY_GUIDE_URI)
    assert identity_guide["mimeType"] == "text/markdown"
    assert "elementId" in identity_guide["description"]

    request_no_model = _request()
    assert _roots_catalog(request_no_model) == []

    model_cache = BuildResult(
        nodes_by_id={"root-1": SimpleNamespace(name="Root 1")},
        root_ids=["root-1", "root-missing"],
        children_by_id={},
        instances_by_type_id={},
        property_to_node={},
        action_to_method={},
    )
    request_roots = _request({"model_cache": model_cache})
    assert _roots_catalog(request_roots) == [
        {"uri": "i3x://roots/root-1", "name": "Root 1"},
        {"uri": "i3x://roots/root-missing", "name": "root-missing"},
    ]
    resources = _resource_catalog(request_roots)
    resource_uris = {resource["uri"] for resource in resources}
    assert MCP_MODEL_OVERVIEW_URI in resource_uris
    assert f"{MCP_ROOT_RESOURCE_PREFIX}root-1" in resource_uris
    assert f"{MCP_ROOT_RESOURCE_PREFIX}root-missing" not in resource_uris

    root_content = json.loads(_read_resource_content(request_roots, f"{MCP_ROOT_RESOURCE_PREFIX}root-1")["text"])
    assert root_content["elementId"] == "root-1"
    assert root_content["displayName"] == "Root 1"
    assert _read_resource_content(request_no_model, MCP_MODEL_OVERVIEW_URI)["mimeType"] == "application/json"
    overview = json.loads(_read_resource_content(request_roots, MCP_MODEL_OVERVIEW_URI)["text"])
    assert overview["modelAvailable"] is True
    assert overview["summary"]["rootCount"] == 1
    assert overview["roots"][0]["uri"] == f"{MCP_ROOT_RESOURCE_PREFIX}root-1"

    with pytest.raises(HTTPException):
        _read_resource_content(request_roots, f"{MCP_ROOT_RESOURCE_PREFIX}not-a-root")


def test_prompt_registry_reads_typed_container() -> None:
    """The typed container replaces the old ``isinstance`` guard: unset reads ``None``."""
    assert _prompt_registry(_request()) is None

    registry = PromptRegistry({})
    assert _prompt_registry(_request({"mcp_prompts": registry})) is registry


def test_read_resource_content_variants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    overrides_dir = tmp_path / "overrides"
    docs_dir = tmp_path / "docs"
    overrides_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)
    (overrides_dir / "mcp_overrides.json").write_text('{"tools":{}}', encoding="utf-8")
    (docs_dir / "QUICK_REFERENCE.md").write_text("# Quick", encoding="utf-8")

    request = _request()
    monkeypatch.setattr("i3x_server.api.mcp.resources._project_root", lambda: tmp_path)
    monkeypatch.setattr("i3x_server.api.mcp.resources.get_prompt", lambda registry, name: {"name": name})

    openapi = _read_resource_content(request, "i3x://openapi")
    assert json.loads(openapi["text"]) == {"openapi": "3.1.0"}
    mapping_guide = _read_resource_content(request, MCP_MAPPING_GUIDE_URI)
    assert mapping_guide["mimeType"] == "text/markdown"
    assert mapping_guide["text"] == MCP_MAPPING_GUIDE
    assert "Plant -> Line -> Pump" in mapping_guide["text"]
    identity_guide = _read_resource_content(request, MCP_IDENTITY_GUIDE_URI)
    assert identity_guide["mimeType"] == "text/markdown"
    assert identity_guide["text"] == MCP_IDENTITY_GUIDE
    assert "first 16 lowercase hexadecimal characters" in identity_guide["text"]
    assert "ns=2;s=Pump-17" in identity_guide["text"]
    assert _read_resource_content(request, "i3x://mcp-overrides")["mimeType"] == "application/json"
    assert json.loads(_read_resource_content(request, "i3x://prompts/demo")["text"])["name"] == "demo"

    with pytest.raises(HTTPException):
        _read_resource_content(request, "i3x://unknown")


def test_read_resource_content_malformed_overrides_stays_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exception text must not flow into the resource response (CodeQL py/stack-trace-exposure)."""
    overrides_dir = tmp_path / "overrides"
    overrides_dir.mkdir(parents=True, exist_ok=True)
    raw = "{this is not valid json"
    (overrides_dir / "mcp_overrides.json").write_text(raw, encoding="utf-8")
    monkeypatch.setattr("i3x_server.api.mcp.resources._project_root", lambda: tmp_path)

    request = _request({"mcp_overrides_applied": True})
    caplog.set_level(logging.WARNING)
    document = json.loads(_read_resource_content(request, "i3x://mcp-overrides")["text"])

    assert document["applied"] is False
    assert document["parseError"] == "mcp_overrides.json is not valid JSON"
    assert document["contents"] == raw
    assert "MCP overrides JSON parse failed" in caplog.text


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
    assert initialized["result"]["protocolVersion"] == "2025-11-25"

    unexpected_request = await _handle_jsonrpc(
        request, {"jsonrpc": "2.0", "id": 3, "method": "notifications/initialized"}
    )
    assert unexpected_request == _jsonrpc_error(3, -32601, "Method not found: notifications/initialized")
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

    monkeypatch.setattr("i3x_server.api.mcp.tools.invoke_mcp_tool", _invoke_ok)
    tool_ok = await _handle_jsonrpc(
        request,
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "tool-a", "arguments": {}}},
    )
    assert tool_ok is not None
    assert tool_ok["result"]["content"][0]["type"] == "text"

    async def _invoke_bad(req: Request, tool: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        del req, tool, arguments
        return {"status_code": 500, "body": {"error": "bad"}}

    monkeypatch.setattr("i3x_server.api.mcp.tools.invoke_mcp_tool", _invoke_bad)
    tool_bad = await _handle_jsonrpc(
        request,
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "tool-a", "arguments": {}}},
    )
    assert tool_bad is not None
    assert tool_bad["result"]["isError"] is True
    assert tool_bad["result"]["structuredContent"] == {"statusCode": 500, "body": {"error": "bad"}}

    unknown_method = await _handle_jsonrpc(request, {"jsonrpc": "2.0", "id": 9, "method": "x/unknown"})
    assert unknown_method == _jsonrpc_error(9, -32601, "Method not found: x/unknown")
