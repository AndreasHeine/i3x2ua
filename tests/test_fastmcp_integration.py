"""
Unit tests for FastMCP server integration in i3x2ua (Issue #124).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from i3x_server.api.mcp import create_fastmcp_server, mount_fastmcp


def test_create_fastmcp_server() -> None:
    app = FastAPI(title="i3x-test")
    mcp = create_fastmcp_server(app)
    assert mcp.name == "i3x"
    assert "Discover elementIds" in mcp.instructions


async def test_fastmcp_resources() -> None:
    app = FastAPI(title="i3x-test")
    mcp = create_fastmcp_server(app)
    
    resource = await mcp.get_resource("i3x://openapi")
    assert resource is not None
    assert resource.name == "OpenAPI specification"
    assert resource.mime_type == "application/json"

    overrides_resource = await mcp.get_resource("i3x://mcp-overrides")
    assert overrides_resource is not None
    assert overrides_resource.name == "MCP overrides"


def test_mount_fastmcp() -> None:
    app = FastAPI(title="i3x-test")
    mcp = mount_fastmcp(app, prefix="/mcp")
    assert mcp is not None
    
    # Verify the mount created an ASGI subapp
    matching_routes = [r for r in app.routes if getattr(r, "path", None) == "/mcp"]
    assert len(matching_routes) == 1
