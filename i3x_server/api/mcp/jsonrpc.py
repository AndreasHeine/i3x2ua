"""JSON-RPC single-message request/response handling for the MCP endpoint.

This module owns the protocol surface: version negotiation constants, legacy transport
sessions, JSON-RPC envelope helpers, modern per-request metadata validation, and the
method dispatch skeleton. Method-specific handlers live in the focused adapters
(``tools.py``, ``prompts.py``, ``resources.py``); the dispatch imports them lazily so the
adapters can import the envelope helpers here without an import cycle.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Request
from pydantic import BaseModel

from i3x_server.version import get_server_version

MCP_PROTOCOL_VERSION = "2026-07-28"
MCP_LEGACY_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
MCP_LEGACY_PROTOCOL_VERSION = MCP_LEGACY_PROTOCOL_VERSIONS[0]
MCP_SUPPORTED_MODERN_PROTOCOL_VERSIONS = (MCP_PROTOCOL_VERSION,)
MCP_SUPPORTED_PROTOCOL_VERSIONS = (MCP_PROTOCOL_VERSION, *MCP_LEGACY_PROTOCOL_VERSIONS)
MCP_META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
MCP_META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
MCP_CACHEABLE_RESULT_METHODS = {
    "tools/list",
    "prompts/list",
    "resources/list",
    "resources/templates/list",
    "resources/read",
}
MCP_ROOT_RESOURCE_PREFIX = "i3x://roots/"
MCP_INSTRUCTIONS = (
    "Discover elementIds before querying values. Value/history maxDepth traverses composition only, "
    "not hierarchy: 1 reads only the object, 2 includes direct components, 0 is unlimited. "
    "For many values below one known container, call readContainerValues with its exact containerId "
    "instead of generating a long elementIds array; it collects properties at any depth below that "
    "container (bounded depth and node count), not only direct components. "
    "Preserve value, quality, and source timestamp; latest known values need not be fresh. "
    "Inspect per-item failures even when HTTP succeeds. PUT/write operations are REST-only. "
    "Subscription tools change monitoring state; acknowledge only updates already processed. "
    "Plant roots are available using getObjectsV1(root=true), not MCP filesystem roots."
)
MCP_SESSION_HEADER = "Mcp-Session-Id"
MCP_SSE_KEEPALIVE_SECONDS = 15
# Legacy transport sessions are state the gateway keeps for the lifetime of the app, so both the
# number of sessions and how long an unused one is retained must be bounded.
MCP_MAX_TRANSPORT_SESSIONS = 200
MCP_SESSION_IDLE_TTL_SECONDS = 900.0


@dataclass(slots=True)
class McpTransportSession:
    protocol_version: str = MCP_LEGACY_PROTOCOL_VERSION
    streams: set[asyncio.Queue[dict[str, Any] | None]] = field(default_factory=set)
    last_used: float = field(default_factory=time.monotonic)


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
    session.last_used = time.monotonic()
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


def _prune_idle_mcp_sessions(sessions: dict[str, McpTransportSession]) -> None:
    """Drop legacy sessions that have no live SSE stream and have been unused for the idle TTL."""
    now = time.monotonic()
    expired = [
        session_id
        for session_id, session in sessions.items()
        if not session.streams and now - session.last_used > MCP_SESSION_IDLE_TTL_SECONDS
    ]
    for session_id in expired:
        sessions.pop(session_id, None)


def _register_mcp_session(sessions: dict[str, McpTransportSession], protocol_version: str) -> str | None:
    """Create a legacy transport session, returning ``None`` when the configured cap is reached."""
    _prune_idle_mcp_sessions(sessions)
    if len(sessions) >= MCP_MAX_TRANSPORT_SESSIONS:
        return None
    session_id = uuid4().hex
    sessions[session_id] = McpTransportSession(protocol_version=protocol_version, last_used=time.monotonic())
    return session_id


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


def _decode_modern_header_value(value: str) -> str | None:
    prefix = "=?base64?"
    if not value.startswith(prefix) or not value.endswith("?="):
        return value
    try:
        return base64.b64decode(value[len(prefix) : -2], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None


def _modern_request_error(request: Request, message: Mapping[str, Any]) -> dict[str, Any] | None:
    params = message.get("params")
    metadata = params.get("_meta") if isinstance(params, Mapping) else None
    metadata = metadata if isinstance(metadata, Mapping) else {}
    protocol_version = metadata.get(MCP_META_PROTOCOL_VERSION)
    protocol_header = request.headers.get("mcp-protocol-version")
    method = message.get("method")
    method_header = request.headers.get("mcp-method")
    name_header = request.headers.get("mcp-name")
    request_id = message.get("id")

    def header_mismatch(detail: str) -> dict[str, Any]:
        return _jsonrpc_error(request_id, -32020, f"HeaderMismatch: {detail}")

    # The protocol version is negotiated before any per-request metadata is validated. A client
    # that only announces an unsupported version - through the header without `_meta`, or through
    # `_meta` without the matching header - must get the documented UnsupportedProtocolVersion
    # answer with the supported list, not a metadata error that says nothing about versions.
    announced_version = protocol_version if isinstance(protocol_version, str) else protocol_header
    if announced_version is not None and announced_version not in MCP_SUPPORTED_MODERN_PROTOCOL_VERSIONS:
        return _jsonrpc_error(
            request_id,
            -32022,
            "Unsupported protocol version",
            {
                "supported": list(MCP_SUPPORTED_PROTOCOL_VERSIONS),
                "requested": announced_version,
            },
        )
    if not isinstance(protocol_version, str) or not isinstance(metadata.get(MCP_META_CLIENT_CAPABILITIES), dict):
        return _jsonrpc_error(request_id, -32602, "Missing or invalid per-request MCP metadata")
    if protocol_header is None or protocol_header != protocol_version:
        return header_mismatch("MCP-Protocol-Version does not match request metadata")
    if not isinstance(method, str) or method_header != method:
        return header_mismatch("Mcp-Method does not match request method")

    params = params if isinstance(params, Mapping) else {}
    expected_name: Any = None
    if method in {"tools/call", "prompts/get"}:
        expected_name = params.get("name")
    elif method == "resources/read":
        expected_name = params.get("uri")
    if isinstance(expected_name, str):
        decoded_name = _decode_modern_header_value(name_header) if name_header is not None else None
        if decoded_name != expected_name:
            return header_mismatch("Mcp-Name does not match request name")
    elif name_header is not None:
        return header_mismatch("Mcp-Name is not applicable to this method")

    return None


def _add_modern_response_metadata(response: dict[str, Any], method: str) -> dict[str, Any]:
    result = response.get("result")
    if not isinstance(result, dict):
        return response
    result.setdefault("resultType", "complete")
    if method in MCP_CACHEABLE_RESULT_METHODS:
        result.setdefault("ttlMs", 0)
        result.setdefault("cacheScope", "private")
    metadata = result.get("_meta")
    if not isinstance(metadata, dict):
        metadata = {}
        result["_meta"] = metadata
    metadata[MCP_META_PROTOCOL_VERSION] = MCP_PROTOCOL_VERSION
    return response


# Handler signature: (request, message id, params) -> complete JSON-RPC response dict.
McpMethodHandler = Callable[[Request, Any, Any], Awaitable[dict[str, Any]]]


@lru_cache(maxsize=1)
def _method_handlers() -> dict[str, McpMethodHandler]:
    """Resolve the method adapters lazily so they can import this module's helpers first."""
    from i3x_server.api.mcp.prompts import handle_prompts_execute, handle_prompts_get, handle_prompts_list
    from i3x_server.api.mcp.resources import handle_resources_list, handle_resources_read
    from i3x_server.api.mcp.tools import handle_tools_call, handle_tools_list

    return {
        "tools/list": handle_tools_list,
        "tools/call": handle_tools_call,
        "prompts/list": handle_prompts_list,
        "prompts/get": handle_prompts_get,
        "prompts/execute": handle_prompts_execute,
        "resources/list": handle_resources_list,
        "resources/read": handle_resources_read,
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
        requested_version = params["protocolVersion"]
        negotiated_version = (
            requested_version if requested_version in MCP_LEGACY_PROTOCOL_VERSIONS else MCP_LEGACY_PROTOCOL_VERSION
        )
        return _jsonrpc_response(
            message_id,
            {
                "protocolVersion": negotiated_version,
                "serverInfo": {"name": "i3x", "version": get_server_version()},
                "capabilities": {
                    "tools": {"listChanged": False},
                    "prompts": {"listChanged": False},
                    "resources": {"listChanged": False},
                },
                "instructions": MCP_INSTRUCTIONS,
            },
        )

    if method == "server/discover":
        return _jsonrpc_response(
            message_id,
            {
                "supportedVersions": list(MCP_SUPPORTED_PROTOCOL_VERSIONS),
                "capabilities": {
                    "tools": {},
                    "prompts": {},
                    "resources": {},
                },
                "_meta": {
                    "io.modelcontextprotocol/serverInfo": {
                        "name": "i3x",
                        "version": get_server_version(),
                    }
                },
                "instructions": MCP_INSTRUCTIONS,
                "ttlMs": 3_600_000,
                "cacheScope": "public",
            },
        )

    if method == "resources/templates/list":
        return _jsonrpc_response(message_id, {"resourceTemplates": []})

    if method == "ping":
        return _jsonrpc_response(message_id, {})

    handler = _method_handlers().get(method)
    if handler is not None:
        return await handler(request, message_id, params)

    return _jsonrpc_error(message_id, -32601, f"Method not found: {method}")


__all__ = ["publish_mcp_notification", "shutdown_mcp_transport"]
