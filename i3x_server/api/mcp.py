from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from i3x_server.errors import i3x_http_error
from i3x_server.mcp import call_mcp_tool, invoke_mcp_tool
from i3x_server.prompts.api import execute_prompt, get_prompt, list_prompt_metadata
from i3x_server.prompts.registry import PromptRegistry

router = APIRouter(prefix="/mcp", tags=["mcp"])


class McpCallRequest(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class McpPromptExecuteRequest(BaseModel):
    name: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class McpResourceReadRequest(BaseModel):
    uri: str


def _jsonrpc_response(result_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": result_id, "result": result}


def _jsonrpc_error(result_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": result_id, "error": error}


def _jsonrpc_notification(method: str, params: Any | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    return payload


def _tool_catalog(request: Request) -> list[dict[str, Any]]:
    tools = getattr(request.app.state, "mcp_tools", {})
    if not isinstance(tools, Mapping):
        return []
    return [
        {"name": tool.name, "description": tool.description, "inputSchema": tool.input_schema}
        for tool in tools.values()
    ]


def _resource_catalog(request: Request) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = [
        {
            "uri": "i3x://openapi",
            "name": "OpenAPI specification",
            "description": "Server OpenAPI JSON document",
            "mimeType": "application/json",
        },
        {
            "uri": "i3x://tool-overrides",
            "name": "MCP tool and prompt overrides",
            "description": "Runtime MCP metadata overrides",
            "mimeType": "application/json",
        },
        {
            "uri": "i3x://docs/quick-reference",
            "name": "Quick reference",
            "description": "Server quick reference documentation",
            "mimeType": "text/markdown",
        },
    ]

    for prompt in list_prompt_metadata(_prompt_registry(request)):
        resources.append(
            {
                "uri": f"i3x://prompts/{prompt['name']}",
                "name": f"Prompt: {prompt['name']}",
                "description": prompt["description"],
                "mimeType": "application/json",
            }
        )

    return resources


def _roots_catalog(request: Request) -> list[dict[str, str]]:
    model_cache = getattr(request.app.state, "model_cache", None)
    if model_cache is None:
        return []

    root_ids = getattr(model_cache, "root_ids", [])
    nodes_by_id = getattr(model_cache, "nodes_by_id", {})
    roots: list[dict[str, str]] = []
    for root_id in root_ids:
        node = nodes_by_id.get(root_id)
        display_name = getattr(node, "name", str(root_id)) if node is not None else str(root_id)
        roots.append(
            {
                "uri": f"i3x://roots/{root_id}",
                "name": display_name,
            }
        )
    return roots


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_resource_content(request: Request, uri: str) -> dict[str, Any]:
    project_root = _project_root()
    if uri == "i3x://openapi":
        return {
            "uri": uri,
            "mimeType": "application/json",
            "text": json.dumps(request.app.openapi(), ensure_ascii=False),
        }

    if uri == "i3x://tool-overrides":
        path = project_root / "tool_overrides.json"
        if not path.exists():
            raise i3x_http_error(404, "Not Found", f"Unknown resource {uri}")
        return {
            "uri": uri,
            "mimeType": "application/json",
            "text": path.read_text(encoding="utf-8"),
        }

    if uri == "i3x://docs/quick-reference":
        path = project_root / "docs" / "QUICK_REFERENCE.md"
        if not path.exists():
            raise i3x_http_error(404, "Not Found", f"Unknown resource {uri}")
        return {
            "uri": uri,
            "mimeType": "text/markdown",
            "text": path.read_text(encoding="utf-8"),
        }

    prompt_prefix = "i3x://prompts/"
    if uri.startswith(prompt_prefix):
        prompt_name = uri.removeprefix(prompt_prefix)
        prompt = get_prompt(_prompt_registry(request), prompt_name)
        return {
            "uri": uri,
            "mimeType": "application/json",
            "text": json.dumps(prompt, ensure_ascii=False),
        }

    raise i3x_http_error(404, "Not Found", f"Unknown resource {uri}")


def _prompt_registry(request: Request) -> PromptRegistry | None:
    registry = getattr(request.app.state, "mcp_prompts", None)
    return registry if isinstance(registry, PromptRegistry) else None


async def _sse_endpoint(request: Request) -> StreamingResponse:
    async def event_stream() -> Any:
        yield "event: endpoint\n"
        yield f"data: {str(request.base_url).rstrip('/')}/mcp\n\n"

        for method in (
            "notifications/prompts/list_changed",
            "notifications/resources/list_changed",
            "notifications/roots/list_changed",
        ):
            notification = _jsonrpc_notification(method, {})
            yield "event: message\n"
            yield f"data: {json.dumps(notification, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


async def _handle_jsonrpc(request: Request, message: dict[str, Any]) -> dict[str, Any] | None:
    jsonrpc = message.get("jsonrpc")
    message_id = message.get("id")
    method = message.get("method")
    params = message.get("params")

    if jsonrpc != "2.0":
        return _jsonrpc_error(message_id, -32600, "Invalid Request")

    if not isinstance(method, str):
        if message_id is None:
            return None
        return _jsonrpc_error(message_id, -32600, "Invalid Request")

    if message_id is None and method.startswith("notifications/"):
        return None

    if message_id is None:
        return None

    if method == "initialize":
        protocol_version = None
        if isinstance(params, dict):
            protocol_version = params.get("protocolVersion")
        return _jsonrpc_response(
            message_id,
            {
                "protocolVersion": protocol_version or "2025-06-18",
                "serverInfo": {"name": "i3x", "version": "1.0"},
                "capabilities": {
                    "tools": {"listChanged": False},
                    "prompts": {"listChanged": True},
                    "resources": {"listChanged": True},
                    "roots": {"listChanged": True},
                },
            },
        )

    if method == "notifications/initialized":
        return None

    if method in {
        "notifications/tools/list_changed",
        "notifications/prompts/list_changed",
        "notifications/resources/list_changed",
        "notifications/roots/list_changed",
    }:
        return None

    if method == "tools/list":
        return _jsonrpc_response(message_id, {"tools": _tool_catalog(request)})

    if method == "tools/call":
        if not isinstance(params, dict):
            return _jsonrpc_error(message_id, -32602, "Invalid params")
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(tool_name, str) or not isinstance(arguments, dict):
            return _jsonrpc_error(message_id, -32602, "Invalid params")
        tools = getattr(request.app.state, "mcp_tools", {})
        if not isinstance(tools, Mapping) or tool_name not in tools:
            return _jsonrpc_error(message_id, -32602, f"Unknown tool {tool_name}")
        tool = tools[tool_name]
        try:
            payload = await invoke_mcp_tool(request, tool, arguments)
        except Exception as exc:
            from fastapi import HTTPException

            if isinstance(exc, HTTPException):
                detail = exc.detail
                error_text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
                return _jsonrpc_error(message_id, int(exc.status_code), error_text)
            raise
        if not isinstance(payload, dict):
            return _jsonrpc_error(message_id, -32603, "Internal error")
        status_code = int(payload.get("status_code", 500))
        body = payload.get("body")
        if status_code >= 400:
            error_text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
            return _jsonrpc_error(message_id, status_code, error_text)
        if isinstance(body, dict) and "text" in body and "content_type" in body:
            content = [{"type": "text", "text": str(body["text"])}]
            return _jsonrpc_response(message_id, {"content": content})
        return _jsonrpc_response(
            message_id, {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}]}
        )

    if method == "prompts/list":
        prompts = list_prompt_metadata(_prompt_registry(request))
        return _jsonrpc_response(message_id, {"prompts": prompts})

    if method == "prompts/get":
        if not isinstance(params, dict):
            return _jsonrpc_error(message_id, -32602, "Invalid params")
        prompt_name = params.get("name")
        if not isinstance(prompt_name, str) or not prompt_name:
            return _jsonrpc_error(message_id, -32602, "Invalid params")
        try:
            prompt = get_prompt(_prompt_registry(request), prompt_name)
        except Exception as exc:
            from fastapi import HTTPException

            if isinstance(exc, HTTPException):
                detail = exc.detail
                error_text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
                return _jsonrpc_error(message_id, int(exc.status_code), error_text)
            raise
        return _jsonrpc_response(message_id, prompt)

    if method == "prompts/execute":
        if not isinstance(params, dict):
            return _jsonrpc_error(message_id, -32602, "Invalid params")
        prompt_name = params.get("name")
        prompt_parameters = params.get("parameters", {})
        if not isinstance(prompt_name, str) or not isinstance(prompt_parameters, dict):
            return _jsonrpc_error(message_id, -32602, "Invalid params")
        try:
            result = execute_prompt(_prompt_registry(request), prompt_name, prompt_parameters)
        except Exception as exc:
            from fastapi import HTTPException

            if isinstance(exc, HTTPException):
                detail = exc.detail
                error_text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
                return _jsonrpc_error(message_id, int(exc.status_code), error_text)
            raise
        return _jsonrpc_response(message_id, result)

    if method == "resources/list":
        return _jsonrpc_response(message_id, {"resources": _resource_catalog(request)})

    if method == "resources/read":
        if not isinstance(params, dict):
            return _jsonrpc_error(message_id, -32602, "Invalid params")
        resource_uri = params.get("uri")
        if not isinstance(resource_uri, str) or not resource_uri:
            return _jsonrpc_error(message_id, -32602, "Invalid params")
        try:
            content = _read_resource_content(request, resource_uri)
        except Exception as exc:
            from fastapi import HTTPException

            if isinstance(exc, HTTPException):
                detail = exc.detail
                error_text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
                return _jsonrpc_error(message_id, int(exc.status_code), error_text)
            raise
        return _jsonrpc_response(message_id, {"contents": [content]})

    if method == "roots/list":
        return _jsonrpc_response(message_id, {"roots": _roots_catalog(request)})

    return _jsonrpc_error(message_id, -32601, f"Method not found: {method}")


@router.get("/tools")
async def list_tools(request: Request) -> dict[str, Any]:
    tools = getattr(request.app.state, "mcp_tools", {})
    if isinstance(tools, Mapping):
        payload: dict[str, dict[str, Any]] = {}
        for name, tool in tools.items():
            item = tool.to_dict()
            # Keep legacy snake_case fields while exposing MCP-compatible camelCase.
            item["inputSchema"] = item.get("input_schema", {})
            item["pathParameters"] = item.get("path_parameters", [])
            item["queryParameters"] = item.get("query_parameters", [])
            item["bodyRequired"] = item.get("body_required", False)
            payload[name] = item
        return {"tools": payload}
    return {"tools": {}}


@router.post("/call")
async def call_tool(request: Request, payload: McpCallRequest) -> Any:
    tools = getattr(request.app.state, "mcp_tools", {})
    if not isinstance(tools, Mapping) or payload.tool not in tools:
        raise i3x_http_error(400, "Bad Request", f"Unknown tool {payload.tool}")

    tool = tools[payload.tool]
    return await call_mcp_tool(request, tool, payload.arguments)


@router.get("/prompts")
async def list_prompts(request: Request) -> dict[str, Any]:
    return {"prompts": list_prompt_metadata(_prompt_registry(request))}


@router.get("/prompts/{name}")
async def get_prompt_definition(request: Request, name: str) -> dict[str, Any]:
    return get_prompt(_prompt_registry(request), name)


@router.post("/prompts/execute")
async def execute_prompt_template(request: Request, payload: McpPromptExecuteRequest) -> dict[str, Any]:
    return execute_prompt(_prompt_registry(request), payload.name, payload.parameters)


@router.get("/resources")
async def list_resources(request: Request) -> dict[str, Any]:
    return {"resources": _resource_catalog(request)}


@router.post("/resources/read")
async def read_resource(request: Request, payload: McpResourceReadRequest) -> dict[str, Any]:
    return {"contents": [_read_resource_content(request, payload.uri)]}


@router.get("/roots")
async def list_roots(request: Request) -> dict[str, Any]:
    return {"roots": _roots_catalog(request)}


@router.get("")
async def mcp_sse(request: Request) -> StreamingResponse:
    return await _sse_endpoint(request)


@router.post("")
async def mcp_post(request: Request) -> JSONResponse:
    message = await request.json()
    if isinstance(message, list):
        if len(message) == 0:
            return JSONResponse(content=_jsonrpc_error(None, -32600, "Invalid Request"))

        responses: list[dict[str, Any]] = []
        for item in message:
            if not isinstance(item, dict):
                responses.append(_jsonrpc_error(None, -32600, "Invalid Request"))
                continue
            response = await _handle_jsonrpc(request, item)
            if response is not None:
                responses.append(response)

        if not responses:
            return JSONResponse(status_code=202, content=None)
        return JSONResponse(content=responses)

    if not isinstance(message, dict):
        return JSONResponse(content=_jsonrpc_error(None, -32600, "Invalid Request"))

    response = await _handle_jsonrpc(request, message)
    if response is None:
        return JSONResponse(status_code=202, content=None)
    return JSONResponse(content=response)
