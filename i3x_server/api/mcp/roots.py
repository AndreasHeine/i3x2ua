"""MCP roots adapter: custom REST plant-root listing."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from i3x_server.api.mcp.jsonrpc import MCP_ROOT_RESOURCE_PREFIX
from i3x_server.application.state import gateway_state

router = APIRouter()


def _roots_catalog(request: Request) -> list[dict[str, str]]:
    model_cache = gateway_state(request).model_cache
    if model_cache is None:
        return []

    roots: list[dict[str, str]] = []
    for root_id in model_cache.root_ids:
        node = model_cache.nodes_by_id.get(root_id)
        display_name = getattr(node, "name", str(root_id)) if node is not None else str(root_id)
        roots.append(
            {
                "uri": f"{MCP_ROOT_RESOURCE_PREFIX}{root_id}",
                "name": display_name,
            }
        )
    return roots


@router.get("/roots", tags=["MCP Helpers"])
async def list_roots(request: Request) -> dict[str, Any]:
    return {"roots": _roots_catalog(request)}
