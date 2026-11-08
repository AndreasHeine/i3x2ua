"""MCP route composition: mounts the focused adapters and the protocol transport routes.

Protocol behaviour lives in ``jsonrpc.py`` (dispatch, sessions, metadata validation);
method semantics live in ``tools.py``/``prompts.py``/``resources.py``/``roots.py``. This
module only wires the routers and the ``GET``/``DELETE``/``POST /mcp`` transport.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from i3x_server.api.mcp import jsonrpc, prompts, resources, roots, tools
from i3x_server.api.mcp.jsonrpc import (
    MCP_LEGACY_PROTOCOL_VERSION,
    MCP_LEGACY_PROTOCOL_VERSIONS,
    MCP_META_PROTOCOL_VERSION,
    MCP_PROTOCOL_VERSION,
    MCP_SESSION_HEADER,
    MCP_SSE_KEEPALIVE_SECONDS,
    McpJsonRpcError,
    McpJsonRpcSuccess,
    McpTransportSession,
    _add_modern_response_metadata,
    _handle_jsonrpc,
    _jsonrpc_error,
    _mcp_sessions,
    _modern_request_error,
    _origin_is_allowed,
    _register_mcp_session,
)

router = APIRouter(prefix="/mcp")
logger = logging.getLogger(__name__)

router.include_router(tools.router)
router.include_router(prompts.router)
router.include_router(resources.router)
router.include_router(roots.router)


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
    session.last_used = time.monotonic()

    async def event_stream() -> AsyncIterator[str]:
        try:
            while True:
                try:
                    # `asyncio.timeout` bounds the wait without cancelling a separate task,
                    # so a notification delivered at the boundary cannot be dropped (L11).
                    async with asyncio.timeout(MCP_SSE_KEEPALIVE_SECONDS):
                        message = await stream.get()
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
    try:
        message = await request.json()
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(content=_jsonrpc_error(None, -32700, "Parse error"))

    if not isinstance(message, dict):
        return JSONResponse(content=_jsonrpc_error(None, -32600, "Invalid Request"))

    params = message.get("params")
    metadata = params.get("_meta") if isinstance(params, dict) else None
    metadata = metadata if isinstance(metadata, dict) else {}
    protocol_version = request.headers.get("mcp-protocol-version")
    modern_request = (
        protocol_version == MCP_PROTOCOL_VERSION
        or MCP_META_PROTOCOL_VERSION in metadata
        or message.get("method") == "server/discover"
        or (protocol_version is not None and protocol_version not in MCP_LEGACY_PROTOCOL_VERSIONS)
    )
    if modern_request:
        validation_error = _modern_request_error(request, message)
        if validation_error is not None:
            return JSONResponse(status_code=400, content=validation_error)
        if message.get("method") == "initialize":
            # JSON-RPC-level errors are delivered as HTTP 200 bodies (like every other
            # `-32601` here); HTTP 400 is reserved for transport validation failures (L11).
            return JSONResponse(
                content=_jsonrpc_error(message.get("id"), -32601, "Method not found: initialize"),
            )
        modern = True
        session_id = None
        sessions = _mcp_sessions(request)
    else:
        modern = False
        if protocol_version is not None and protocol_version not in MCP_LEGACY_PROTOCOL_VERSIONS:
            return JSONResponse(
                status_code=400,
                content=_jsonrpc_error(None, -32600, "Unsupported protocol version"),
            )
        session_id = request.headers.get(MCP_SESSION_HEADER)
        sessions = _mcp_sessions(request)
        if session_id is not None and session_id not in sessions:
            return JSONResponse(status_code=404, content=_jsonrpc_error(None, -32600, "Unknown MCP session"))
        if session_id is not None and protocol_version is not None:
            if protocol_version != sessions[session_id].protocol_version:
                return JSONResponse(
                    status_code=400,
                    content=_jsonrpc_error(None, -32600, "MCP protocol version does not match session"),
                )

    try:
        response = await _handle_jsonrpc(request, message)
    except Exception:
        logger.exception("MCP request failed method=%s", message.get("method"))
        response = _jsonrpc_error(message.get("id"), -32603, "Internal error") if "id" in message else None
    if modern and response is not None and "result" in response:
        response = _add_modern_response_metadata(response, message["method"])
    response_headers: dict[str, str] = {}
    if not modern and session_id is not None:
        # Every legacy request refreshes the session; idle sessions without a stream are pruned
        # when a new initialize arrives.
        existing = sessions.get(session_id)
        if isinstance(existing, McpTransportSession):
            existing.last_used = time.monotonic()
    if not modern and message.get("method") == "initialize" and response is not None and "result" in response:
        negotiated_version = response["result"].get("protocolVersion", MCP_LEGACY_PROTOCOL_VERSION)
        session_id = _register_mcp_session(sessions, str(negotiated_version))
        if session_id is None:
            logger.warning(
                "MCP transport session limit reached count=%d limit=%d",
                len(sessions),
                jsonrpc.MCP_MAX_TRANSPORT_SESSIONS,
            )
            return JSONResponse(
                status_code=400,
                content=_jsonrpc_error(
                    message.get("id"),
                    -32000,
                    "Too many MCP transport sessions",
                    {"maxSessions": jsonrpc.MCP_MAX_TRANSPORT_SESSIONS},
                ),
            )
        response_headers[MCP_SESSION_HEADER] = session_id
    elif session_id is not None and message.get("method") == "notifications/initialized":
        response_headers[MCP_SESSION_HEADER] = session_id
    if response is None:
        return Response(status_code=202, headers=response_headers)
    return JSONResponse(content=response, headers=response_headers)
