from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field
from typing_extensions import Never

from i3x_server.errors import i3x_http_error
from i3x_server.mcp import call_mcp_tool, invoke_mcp_tool
from i3x_server.prompts.api import execute_prompt, get_prompt, list_prompt_metadata
from i3x_server.prompts.registry import PromptRegistry
from i3x_server.version import get_server_version

router = APIRouter(prefix="/mcp", tags=["MCP"])
logger = logging.getLogger(__name__)
MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_INSTRUCTIONS = (
    "Discover elementIds before querying values. Value/history maxDepth traverses composition only, "
    "not hierarchy: 1 reads only the object, 2 includes direct components, 0 is unlimited. "
    "Preserve value, quality, and source timestamp; latest known values need not be fresh. "
    "Inspect per-item failures even when HTTP succeeds. PUT/write operations are REST-only. "
    "Subscription tools change monitoring state; acknowledge only updates already processed. "
    "Plant roots are available using getObjectsV1(root=true), not MCP filesystem roots."
    " Use findAssets for explicit candidates, discoverAssetContext for bounded graph context, "
    "getAssetSnapshot for non-atomic VQT evidence, and startAssetMonitoring for create/register with rollback. "
    "summarizeSignalHistory reads bounded pages; diagnoseDataAvailability reports evidence, not health verdicts."
)


class McpCallRequest(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class McpPromptExecuteRequest(BaseModel):
    name: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class McpResourceReadRequest(BaseModel):
    uri: str


class McpJsonRpcErrorDetail(BaseModel):
    code: int
    message: str
    data: Any = None


class McpJsonRpcSuccess(BaseModel):
    jsonrpc: Literal["2.0"]
    id: Any
    result: Any


class McpJsonRpcError(BaseModel):
    jsonrpc: Literal["2.0"]
    id: Any
    error: McpJsonRpcErrorDetail


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


def _raise_invalid_tool(tool_name: str) -> Never:
    raise i3x_http_error(400, "Bad Request", f"Unknown tool {tool_name}")


def _raise_invalid_resource(uri: str) -> Never:
    raise i3x_http_error(404, "Not Found", f"Unknown resource {uri}")


def _raise_mcp_error(operation: str, cause: Exception) -> Never:
    raise i3x_http_error(500, "Internal Server Error", f"MCP {operation} failed: {str(cause)}")


def _tool_catalog(request: Request) -> list[dict[str, Any]]:
    tools = getattr(request.app.state, "mcp_tools", {})
    if not isinstance(tools, Mapping):
        return []
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.input_schema,
            "outputSchema": tool.output_schema,
            "annotations": tool.annotations,
        }
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
            "uri": "i3x://mcp-overrides",
            "name": "MCP overrides",
            "description": "Runtime MCP tool, prompt, and feature overrides",
            "mimeType": "application/json",
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
    return Path(__file__).resolve().parents[3]


def _read_resource_content(request: Request, uri: str) -> dict[str, Any]:
    project_root = _project_root()
    if uri == "i3x://openapi":
        return {
            "uri": uri,
            "mimeType": "application/json",
            "text": json.dumps(request.app.openapi(), ensure_ascii=False),
        }

    if uri == "i3x://mcp-overrides":
        path = project_root / "overrides" / "mcp_overrides.json"
        if not path.exists():
            _raise_invalid_resource(uri)
        return {
            "uri": uri,
            "mimeType": "application/json",
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

    _raise_invalid_resource(uri)


def _prompt_registry(request: Request) -> PromptRegistry | None:
    registry = getattr(request.app.state, "mcp_prompts", None)
    return registry if isinstance(registry, PromptRegistry) else None


def _tool_result(body: Any, status_code: int) -> dict[str, Any]:
    failed = status_code >= 400
    if isinstance(body, dict):
        results = body.get("results")
        failed = (
            failed
            or body.get("success") is False
            or any(
                isinstance(item, dict) and item.get("success") is False
                for item in (results if isinstance(results, list) else [])
            )
        )
    return {
        "content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}],
        "structuredContent": {"statusCode": status_code, "body": body},
        "isError": failed,
    }


async def _handle_jsonrpc(request: Request, message: dict[str, Any]) -> dict[str, Any] | None:
    jsonrpc = message.get("jsonrpc")
    message_id = message.get("id")
    method = message.get("method")
    params = message.get("params")

    if "id" in message and type(message_id) not in (str, int):
        return _jsonrpc_error(None, -32600, "Invalid Request")
    if jsonrpc != "2.0":
        return _jsonrpc_error(message_id, -32600, "Invalid Request")

    if not isinstance(method, str):
        return _jsonrpc_error(message_id, -32600, "Invalid Request")

    if message_id is None:
        return None
    if params is not None and not isinstance(params, dict):
        return _jsonrpc_error(message_id, -32602, "Invalid params")

    if method == "initialize":
        if not isinstance(params, dict) or not isinstance(params.get("protocolVersion"), str):
            return _jsonrpc_error(message_id, -32602, "protocolVersion must be a string")
        return _jsonrpc_response(
            message_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "serverInfo": {"name": "i3x", "version": get_server_version()},
                "capabilities": {
                    "tools": {"listChanged": False},
                    "prompts": {"listChanged": False},
                    "resources": {"listChanged": False},
                },
                "instructions": MCP_INSTRUCTIONS,
            },
        )

    if method == "resources/templates/list":
        return _jsonrpc_response(message_id, {"resourceTemplates": []})

    if method == "ping":
        return _jsonrpc_response(message_id, {})

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
        validation_error = next(Draft202012Validator(tool.input_schema).iter_errors(arguments), None)
        if validation_error is not None:
            return _jsonrpc_error(message_id, -32602, f"Invalid tool arguments: {validation_error.message}")
        try:
            payload = await invoke_mcp_tool(request, tool, arguments)
        except HTTPException as exc:
            if exc.status_code in {400, 422}:
                return _jsonrpc_error(message_id, -32602, "Invalid tool arguments", exc.detail)
            return _jsonrpc_response(message_id, _tool_result(exc.detail, exc.status_code))
        if not isinstance(payload, dict) or type(payload.get("status_code")) is not int or "body" not in payload:
            logger.error("Invalid MCP tool response tool=%s", tool_name)
            return _jsonrpc_error(message_id, -32603, "Internal error")
        status_code = payload["status_code"]
        body = payload["body"]
        return _jsonrpc_response(message_id, _tool_result(body, status_code))

    if method == "prompts/list":
        prompts: list[dict[str, Any]] = [dict(item) for item in list_prompt_metadata(_prompt_registry(request))]
        registry = _prompt_registry(request)
        for metadata in prompts:
            prompt = registry.get(metadata["name"]) if registry is not None else None
            if prompt is not None:
                metadata["arguments"] = [{"name": name, "required": True} for name in prompt.inputs]
        return _jsonrpc_response(message_id, {"prompts": prompts})

    if method == "prompts/get":
        if not isinstance(params, dict):
            return _jsonrpc_error(message_id, -32602, "Invalid params")
        prompt_name = params.get("name")
        arguments = params.get("arguments", {})
        if (
            not isinstance(prompt_name, str)
            or not prompt_name
            or not isinstance(arguments, dict)
            or not all(isinstance(value, str) for value in arguments.values())
        ):
            return _jsonrpc_error(message_id, -32602, "Invalid params")
        try:
            rendered = execute_prompt(_prompt_registry(request), prompt_name, arguments)
        except HTTPException as exc:
            return _jsonrpc_error(message_id, -32602, "Invalid prompt request", exc.detail)
        return _jsonrpc_response(
            message_id,
            {
                "description": rendered["description"],
                "messages": [{"role": "user", "content": {"type": "text", "text": rendered["rendered"]}}],
            },
        )

    if method == "prompts/execute":
        if not isinstance(params, dict):
            return _jsonrpc_error(message_id, -32602, "Invalid params")
        prompt_name = params.get("name")
        prompt_parameters = params.get("parameters", {})
        if not isinstance(prompt_name, str) or not isinstance(prompt_parameters, dict):
            return _jsonrpc_error(message_id, -32602, "Invalid params")
        try:
            prompt_result = execute_prompt(_prompt_registry(request), prompt_name, prompt_parameters)
        except HTTPException as exc:
            return _jsonrpc_error(message_id, -32602, "Invalid prompt request", exc.detail)
        return _jsonrpc_response(message_id, prompt_result)

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
        except HTTPException as exc:
            return _jsonrpc_error(message_id, -32002, "Resource not found", exc.detail)
        return _jsonrpc_response(message_id, {"contents": [content]})

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
        _raise_invalid_tool(payload.tool)

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


@router.post(
    "",
    response_class=JSONResponse,
    responses={
        200: {
            "model": McpJsonRpcSuccess | McpJsonRpcError,
            "description": "JSON-RPC 2.0 response object. Protocol errors are JSON-RPC error "
            "objects, not i3X error envelopes.",
            "content": {
                "application/json": {
                    "example": {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {"tools": []},
                    },
                }
            },
        },
        202: {
            "description": "Notification accepted; the HTTP response has no body.",
            "content": {},
        },
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"type": "object"},
                    "examples": {
                        "request": {
                            "summary": "List tools",
                            "value": {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                        },
                        "notification": {
                            "summary": "Initialized notification (HTTP 202)",
                            "value": {
                                "jsonrpc": "2.0",
                                "method": "notifications/initialized",
                            },
                        },
                    },
                }
            },
        },
    },
)
async def mcp_post(request: Request) -> Response:
    origin = request.headers.get("origin")
    allowed_origins = getattr(request.app.state, "mcp_allowed_origins", [])
    if origin is not None:
        same_local_origin = origin == str(request.base_url).rstrip("/") and urlsplit(origin).hostname in {
            "localhost",
            "127.0.0.1",
            "::1",
        }
        if not same_local_origin and (origin == "*" or origin not in allowed_origins):
            return JSONResponse(status_code=403, content=_jsonrpc_error(None, -32600, "Origin not allowed"))
    protocol_version = request.headers.get("mcp-protocol-version")
    if protocol_version is not None and protocol_version != MCP_PROTOCOL_VERSION:
        return JSONResponse(status_code=400, content=_jsonrpc_error(None, -32600, "Unsupported protocol version"))
    try:
        message = await request.json()
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(content=_jsonrpc_error(None, -32700, "Parse error"))

    if not isinstance(message, dict):
        return JSONResponse(content=_jsonrpc_error(None, -32600, "Invalid Request"))

    try:
        response = await _handle_jsonrpc(request, message)
    except Exception:
        logger.exception("MCP request failed method=%s", message.get("method"))
        response = _jsonrpc_error(message.get("id"), -32603, "Internal error") if "id" in message else None
    if response is None:
        return Response(status_code=202)
    return JSONResponse(content=response)
