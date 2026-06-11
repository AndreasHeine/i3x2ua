from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from i3x_server.mcp import call_mcp_tool

router = APIRouter(prefix="/mcp", tags=["mcp"])


class McpCallRequest(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


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
        from i3x_server.errors import i3x_http_error

        raise i3x_http_error(400, "Bad Request", f"Unknown tool {payload.tool}")

    tool = tools[payload.tool]
    return await call_mcp_tool(request, tool, payload.arguments)