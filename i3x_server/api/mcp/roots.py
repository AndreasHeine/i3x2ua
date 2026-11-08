"""MCP roots adapter: custom REST plant-root listing."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from i3x_server.api.mcp.jsonrpc import MCP_ROOT_RESOURCE_PREFIX

router = APIRouter()


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
                "uri": f"{MCP_ROOT_RESOURCE_PREFIX}{root_id}",
                "name": display_name,
            }
        )
    return roots


@router.get("/roots", tags=["MCP Helpers"])
async def list_roots(request: Request) -> dict[str, Any]:
    return {"roots": _roots_catalog(request)}
