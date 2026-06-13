from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from i3x_server.errors import i3x_http_error
from i3x_server.mcp import call_mcp_tool, invoke_mcp_tool

router = APIRouter(prefix="/mcp", tags=["mcp"])


class McpCallRequest(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


def _jsonrpc_response(result_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": result_id, "result": result}


def _jsonrpc_error(result_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": result_id, "error": error}


def _tool_catalog(request: Request) -> list[dict[str, Any]]:
    tools = getattr(request.app.state, "mcp_tools", {})
    if not isinstance(tools, Mapping):
        return []
    return [
        {"name": tool.name, "description": tool.description, "inputSchema": tool.input_schema}
        for tool in tools.values()
    ]


async def _sse_endpoint(request: Request) -> StreamingResponse:
    async def event_stream() -> Any:
        yield "event: endpoint\n"
        yield f"data: {str(request.base_url).rstrip('/')}/mcp\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


async def _handle_jsonrpc(request: Request, message: dict[str, Any]) -> dict[str, Any] | None:
    message_id = message.get("id")
    method = message.get("method")
    params = message.get("params")
    if not isinstance(method, str):
        if message_id is None:
            return None
        return _jsonrpc_error(message_id, -32600, "Invalid Request")

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
                "capabilities": {"tools": {"listChanged": False}},
            },
        )

    if method == "notifications/initialized":
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

    return _jsonrpc_error(message_id, -32601, f"Method not found: {method}")


@router.get("/tools")
async def list_tools(request: Request) -> dict[str, Any]:
    tools = getattr(request.app.state, "mcp_tools", {})
    if isinstance(tools, Mapping):
        return {"tools": {name: tool.to_dict() for name, tool in tools.items()}}
    return {"tools": {}}


@router.post("/call")
async def call_tool(request: Request, payload: McpCallRequest) -> Any:
    tools = getattr(request.app.state, "mcp_tools", {})
    if not isinstance(tools, Mapping) or payload.tool not in tools:
        raise i3x_http_error(400, "Bad Request", f"Unknown tool {payload.tool}")

    tool = tools[payload.tool]
    return await call_mcp_tool(request, tool, payload.arguments)


@router.get("")
async def mcp_sse(request: Request) -> StreamingResponse:
    return await _sse_endpoint(request)


@router.post("")
async def mcp_post(request: Request) -> JSONResponse:
    message = await request.json()
    if not isinstance(message, dict):
        raise i3x_http_error(400, "Bad Request", "Invalid JSON-RPC message")
    response = await _handle_jsonrpc(request, message)
    if response is None:
        return JSONResponse(status_code=202, content=None)
    return JSONResponse(content=response)
