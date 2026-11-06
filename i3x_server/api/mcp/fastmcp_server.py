"""
FastMCP integration module for i3x2ua.

This module provides the FastMCP server implementation, allowing i3x2ua to expose
its Model Context Protocol (MCP) endpoints using the modern FastMCP framework
mounted directly into FastAPI, replacing legacy manual JSON-RPC dispatchers.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastmcp import FastMCP

from i3x_server.version import get_server_version

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


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def create_fastmcp_server(app: FastAPI | None = None) -> FastMCP:
    """
    Create and configure a FastMCP server instance for i3x2ua.
    
    Args:
        app: Optional FastAPI parent application to link OpenAPI and state.
        
    Returns:
        Configured FastMCP instance ready for mounting or standalone execution.
    """
    server_version = get_server_version()
    mcp = FastMCP(
        name="i3x",
        instructions=MCP_INSTRUCTIONS,
        version=server_version,
    )

    # Register standard OpenAPI resource
    @mcp.resource(
        "i3x://openapi",
        name="OpenAPI specification",
        description="Server OpenAPI JSON document",
        mime_type="application/json",
    )
    def openapi_resource() -> str:
        if app is not None:
            return json.dumps(app.openapi(), ensure_ascii=False)
        return "{}"

    # Register runtime MCP overrides resource
    @mcp.resource(
        "i3x://mcp-overrides",
        name="MCP overrides",
        description="Runtime MCP tool, prompt, and feature overrides",
        mime_type="application/json",
    )
    def overrides_resource() -> str:
        path = _project_root() / "overrides" / "mcp_overrides.json"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return "{}"

    return mcp


def mount_fastmcp(app: FastAPI, prefix: str = "/mcp") -> FastMCP:
    """
    Mount the FastMCP ASGI sub-application onto a FastAPI application.
    
    Args:
        app: The parent FastAPI application.
        prefix: URL path prefix to mount the MCP server (default: /mcp).
        
    Returns:
        The mounted FastMCP server instance.
    """
    mcp = create_fastmcp_server(app)
    mcp_app = mcp.http_app(path="/", stateless_http=True, json_response=True)
    app.mount(prefix, mcp_app)
    logger.info("FastMCP server mounted at %s", prefix)
    return mcp
