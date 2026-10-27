from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field
from typing_extensions import Never

from i3x_server.errors import i3x_http_error
from i3x_server.mcp.invocation import call_mcp_tool, invoke_mcp_tool
from i3x_server.prompts.api import execute_prompt, get_prompt, list_prompt_metadata
from i3x_server.prompts.registry import PromptRegistry
from i3x_server.version import get_server_version

router = APIRouter(prefix="/mcp")
logger = logging.getLogger(__name__)
MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_INSTRUCTIONS = (
    "Discover elementIds before querying values. Value/history maxDepth traverses composition only, "
    "not hierarchy: 1 reads only the object, 2 includes direct components, 0 is unlimited. "
    "For many values below one known container, call readContainerValues with its exact containerId "
    "instead of generating a long elementIds array. "
    "Preserve value, quality, and source timestamp; latest known values need not be fresh. "
    "Inspect per-item failures even when HTTP succeeds. PUT/write operations are REST-only. "
    "Subscription tools change monitoring state; acknowledge only updates already processed. "
    "Plant roots are available using getObjectsV1(root=true), not MCP filesystem roots."
)
MCP_SESSION_HEADER = "Mcp-Session-Id"
MCP_SSE_KEEPALIVE_SECONDS = 15


@dataclass(slots=True)
class McpTransportSession:
    streams: set[asyncio.Queue[dict[str, Any] | None]] = field(default_factory=set)


def publish_mcp_notification(
    app: FastAPI,
    session_id: str,
    method: str,
    params: dict[str, Any] | None = None,
) -> int:
    if not method.startswith("notifications/") or len(method) == len("notifications/"):
        raise ValueError("MCP server notifications must use a notifications/ method")
    sessions = getattr(app.state, "mcp_transport_sessions", {})
    session = sessions.get(session_id) if isinstance(sessions, dict) else None
    if not isinstance(session, McpTransportSession):
        return 0
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    for stream in tuple(session.streams):
        stream.put_nowait(message)
    return len(session.streams)


def shutdown_mcp_transport(app: FastAPI) -> None:
    sessions = getattr(app.state, "mcp_transport_sessions", {})
    if not isinstance(sessions, dict):
        return
    for session in sessions.values():
        if isinstance(session, McpTransportSession):
            for stream in tuple(session.streams):
                stream.put_nowait(None)
    sessions.clear()


def _mcp_sessions(request: Request) -> dict[str, McpTransportSession]:
    sessions = getattr(request.app.state, "mcp_transport_sessions", None)
    if not isinstance(sessions, dict):
        sessions = {}
        request.app.state.mcp_transport_sessions = sessions
    return sessions


def _origin_is_allowed(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True
    allowed_origins = getattr(request.app.state, "mcp_allowed_origins", [])
    same_local_origin = origin == str(request.base_url).rstrip("/") and urlsplit(origin).hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }
    return same_local_origin or (origin != "*" and origin in allowed_origins)


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


def _raise_invalid_tool(tool_name: str) -> Never:
    raise i3x_http_error(400, "Bad Request", f"Unknown tool {tool_name}")


def _raise_invalid_resource(uri: str) -> Never:
    raise i3x_http_error(404, "Not Found", f"Unknown resource {uri}")


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


def _compact_object(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    return {key: item[key] for key in ("elementId", "displayName") if key in item}


def _compact_bulk_results(body: dict[str, Any], project_result: Any) -> dict[str, Any]:
    results = body.get("results")
    if not isinstance(results, list):
        return body
    compacted = dict(body)
    compacted["results"] = [
        {
            **item,
            **({"result": project_result(item["result"])} if item.get("success") and "result" in item else {}),
        }
        if isinstance(item, dict)
        else item
        for item in results
    ]
    return compacted


def _compact_tool_body(tool_name: str, body: Any) -> Any:
    if not isinstance(body, dict):
        return body

    if tool_name == "getObjectsV1" and isinstance(body.get("result"), list):
        compacted = dict(body)
        compacted["result"] = [_compact_object(item) for item in body["result"]]
        return compacted

    if tool_name == "listObjectsByIdV1":
        return _compact_bulk_results(body, _compact_object)

    if tool_name == "queryRelatedObjectsV1":

        def compact_related(result: Any) -> Any:
            if not isinstance(result, list):
                return result
            return [
                {
                    **related,
                    **({"object": _compact_object(related["object"])} if "object" in related else {}),
                }
                if isinstance(related, dict)
                else related
                for related in result
            ]

        return _compact_bulk_results(body, compact_related)

    if tool_name == "getObjectTypesV1" and isinstance(body.get("result"), list):
        compacted = dict(body)
        compacted["result"] = [
            {key: item[key] for key in ("elementId", "displayName", "namespaceUri", "version") if key in item}
            if isinstance(item, dict)
            else item
            for item in body["result"]
        ]
        return compacted

    if tool_name == "queryObjectTypesV1":
        return _compact_bulk_results(
            body,
            lambda result: (
                {key: value for key, value in result.items() if key != "related"}
                if isinstance(result, dict)
                else result
            ),
        )

    if tool_name in {"getRelationshipTypesV1", "queryRelationshipTypesV1"}:
        if tool_name == "getRelationshipTypesV1" and isinstance(body.get("result"), list):
            compacted = dict(body)
            compacted["result"] = [_compact_object(item) for item in body["result"]]
            return compacted
        if tool_name == "queryRelationshipTypesV1":
            return _compact_bulk_results(body, _compact_object)

    if tool_name == "queryLastKnownValuesV1":

        def compact_current(result: Any) -> Any:
            if not isinstance(result, dict):
                return result
            return {
                key: value
                for key, value in result.items()
                if key != "isComposition" and not (key == "components" and value is None)
            }

        return _compact_bulk_results(body, compact_current)

    if tool_name == "queryHistoricalValuesV1":

        def compact_history(result: Any) -> Any:
            if not isinstance(result, dict):
                return result
            return {
                key: value
                for key, value in result.items()
                if key != "isComposition" and not (key == "components" and value is None)
            }

        return _compact_bulk_results(body, compact_history)

    return body


def _tool_result(body: Any, status_code: int, tool_name: str | None = None) -> dict[str, Any]:
    if tool_name is not None:
        body = _compact_tool_body(tool_name, body)
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
        return _jsonrpc_response(message_id, _tool_result(body, status_code, tool_name))

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


@router.get("/tools", tags=["MCP Helpers"])
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


@router.post("/call", tags=["MCP Helpers"])
async def call_tool(request: Request, payload: McpCallRequest) -> Any:
    tools = getattr(request.app.state, "mcp_tools", {})
    if not isinstance(tools, Mapping) or payload.tool not in tools:
        _raise_invalid_tool(payload.tool)

    tool = tools[payload.tool]
    return await call_mcp_tool(request, tool, payload.arguments)


@router.get("/prompts", tags=["MCP Helpers"])
async def list_prompts(request: Request) -> dict[str, Any]:
    return {"prompts": list_prompt_metadata(_prompt_registry(request))}


@router.get("/prompts/{name}", tags=["MCP Helpers"])
async def get_prompt_definition(request: Request, name: str) -> dict[str, Any]:
    return get_prompt(_prompt_registry(request), name)


@router.post("/prompts/execute", tags=["MCP Helpers"])
async def execute_prompt_template(request: Request, payload: McpPromptExecuteRequest) -> dict[str, Any]:
    return execute_prompt(_prompt_registry(request), payload.name, payload.parameters)


@router.get("/resources", tags=["MCP Helpers"])
async def list_resources(request: Request) -> dict[str, Any]:
    return {"resources": _resource_catalog(request)}


@router.post("/resources/read", tags=["MCP Helpers"])
async def read_resource(request: Request, payload: McpResourceReadRequest) -> dict[str, Any]:
    return {"contents": [_read_resource_content(request, payload.uri)]}


@router.get("/roots", tags=["MCP Helpers"])
async def list_roots(request: Request) -> dict[str, Any]:
    return {"roots": _roots_catalog(request)}


@router.get(
    "",
    tags=["MCP Protocol"],
    responses={
        200: {
            "description": "Session-scoped server-sent event stream.",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        400: {"description": "Missing MCP session identifier."},
        404: {"description": "Unknown MCP session."},
        406: {"description": "The Accept header must include text/event-stream."},
    },
)
async def mcp_get(request: Request) -> Response:
    if not _origin_is_allowed(request):
        return JSONResponse(status_code=403, content=_jsonrpc_error(None, -32600, "Origin not allowed"))
    accepted_types = {
        item.split(";", maxsplit=1)[0].strip().lower() for item in request.headers.get("accept", "").split(",")
    }
    if "text/event-stream" not in accepted_types:
        return JSONResponse(status_code=406, content=_jsonrpc_error(None, -32600, "Accept: text/event-stream required"))

    session_id = request.headers.get(MCP_SESSION_HEADER)
    if not session_id:
        return JSONResponse(status_code=400, content=_jsonrpc_error(None, -32600, "Mcp-Session-Id required"))
    session = _mcp_sessions(request).get(session_id)
    if session is None:
        return JSONResponse(status_code=404, content=_jsonrpc_error(None, -32600, "Unknown MCP session"))

    stream: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    session.streams.add(stream)

    async def event_stream() -> AsyncIterator[str]:
        try:
            while True:
                try:
                    message = await asyncio.wait_for(stream.get(), timeout=MCP_SSE_KEEPALIVE_SECONDS)
                except TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": keepalive\n\n"
                    continue
                if message is None:
                    break
                yield f"event: message\ndata: {json.dumps(message, ensure_ascii=False)}\n\n"
        finally:
            session.streams.discard(stream)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            MCP_SESSION_HEADER: session_id,
        },
    )


@router.delete("", tags=["MCP Protocol"], status_code=200)
async def mcp_delete(request: Request) -> Response:
    if not _origin_is_allowed(request):
        return JSONResponse(status_code=403, content=_jsonrpc_error(None, -32600, "Origin not allowed"))
    session_id = request.headers.get(MCP_SESSION_HEADER)
    if not session_id:
        return JSONResponse(status_code=400, content=_jsonrpc_error(None, -32600, "Mcp-Session-Id required"))
    session = _mcp_sessions(request).pop(session_id, None)
    if session is None:
        return JSONResponse(status_code=404, content=_jsonrpc_error(None, -32600, "Unknown MCP session"))
    for stream in tuple(session.streams):
        stream.put_nowait(None)
    return Response(status_code=200)


@router.post(
    "",
    tags=["MCP Protocol"],
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
    if not _origin_is_allowed(request):
        return JSONResponse(status_code=403, content=_jsonrpc_error(None, -32600, "Origin not allowed"))
    protocol_version = request.headers.get("mcp-protocol-version")
    if protocol_version is not None and protocol_version != MCP_PROTOCOL_VERSION:
        return JSONResponse(status_code=400, content=_jsonrpc_error(None, -32600, "Unsupported protocol version"))
    session_id = request.headers.get(MCP_SESSION_HEADER)
    sessions = _mcp_sessions(request)
    if session_id is not None and session_id not in sessions:
        return JSONResponse(status_code=404, content=_jsonrpc_error(None, -32600, "Unknown MCP session"))
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
    response_headers: dict[str, str] = {}
    if message.get("method") == "initialize" and response is not None and "result" in response:
        session_id = uuid4().hex
        sessions[session_id] = McpTransportSession()
        response_headers[MCP_SESSION_HEADER] = session_id
    elif session_id is not None and message.get("method") == "notifications/initialized":
        response_headers[MCP_SESSION_HEADER] = session_id
    if response is None:
        return Response(status_code=202, headers=response_headers)
    return JSONResponse(content=response, headers=response_headers)
