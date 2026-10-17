"""Aggregate the MCP protocol/helpers and curated workflow endpoints."""

from fastapi import APIRouter

from i3x_server.api.mcp.composites import router as composite_router
from i3x_server.api.mcp.monolithic import router as protocol_router

router = APIRouter()
router.include_router(protocol_router)
router.include_router(composite_router)

__all__ = ["router"]
