"""MCP prompts adapter: ``prompts/list``/``prompts/get``/``prompts/execute`` handlers and helper routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from i3x_server.api.mcp.jsonrpc import _jsonrpc_error, _jsonrpc_response
from i3x_server.prompts.api import execute_prompt, get_prompt, list_prompt_metadata
from i3x_server.prompts.registry import PromptRegistry

router = APIRouter()


class McpPromptExecuteRequest(BaseModel):
    name: str
    parameters: dict[str, Any] = Field(default_factory=dict)


def _prompt_registry(request: Request) -> PromptRegistry | None:
    registry = getattr(request.app.state, "mcp_prompts", None)
    return registry if isinstance(registry, PromptRegistry) else None


async def handle_prompts_list(request: Request, message_id: Any, params: Any) -> dict[str, Any]:
    prompts: list[dict[str, Any]] = [dict(item) for item in list_prompt_metadata(_prompt_registry(request))]
    registry = _prompt_registry(request)
    for metadata in prompts:
        prompt = registry.get(metadata["name"]) if registry is not None else None
        if prompt is not None:
            metadata["arguments"] = [{"name": name, "required": True} for name in prompt.inputs]
    return _jsonrpc_response(message_id, {"prompts": prompts})


async def handle_prompts_get(request: Request, message_id: Any, params: Any) -> dict[str, Any]:
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


async def handle_prompts_execute(request: Request, message_id: Any, params: Any) -> dict[str, Any]:
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


@router.get("/prompts", tags=["MCP Helpers"])
async def list_prompts(request: Request) -> dict[str, Any]:
    return {"prompts": list_prompt_metadata(_prompt_registry(request))}


@router.get("/prompts/{name}", tags=["MCP Helpers"])
async def get_prompt_definition(request: Request, name: str) -> dict[str, Any]:
    return get_prompt(_prompt_registry(request), name)


@router.post("/prompts/execute", tags=["MCP Helpers"])
async def execute_prompt_template(request: Request, payload: McpPromptExecuteRequest) -> dict[str, Any]:
    return execute_prompt(_prompt_registry(request), payload.name, payload.parameters)
