"""MCP tools adapter: ``tools/list``/``tools/call`` protocol handlers and helper routes."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any, Never

from fastapi import APIRouter, HTTPException, Request
from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field

from i3x_server.api.mcp.jsonrpc import _jsonrpc_error, _jsonrpc_response
from i3x_server.errors import i3x_http_error
from i3x_server.mcp.invocation import call_mcp_tool, invoke_mcp_tool

router = APIRouter()
logger = logging.getLogger(__name__)


class McpCallRequest(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


def _raise_invalid_tool(tool_name: str) -> Never:
    raise i3x_http_error(400, "Bad Request", f"Unknown tool {tool_name}")


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
        # `outputSchema` declares an object; a non-object body (array/scalar from a
        # route that returns one) would violate it, so wrap it under a stable key (L11).
        return {"data": body}

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


async def handle_tools_list(request: Request, message_id: Any, params: Any) -> dict[str, Any]:
    return _jsonrpc_response(message_id, {"tools": _tool_catalog(request)})


async def handle_tools_call(request: Request, message_id: Any, params: Any) -> dict[str, Any]:
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
