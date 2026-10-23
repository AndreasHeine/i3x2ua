"""
MCP protocol endpoint package.

This module re-exports the router from the MCP protocol implementation.

During Phase 3+, this package will gradually absorb protocol surfaces into
focused modules:
  - jsonrpc.py → JSON-RPC single-message request/response handling
  - tools.py → tools/list, tools/call
  - prompts.py → prompts/list, prompts/get, prompts/execute
  - resources.py → resources/list, resources/read
  - roots.py → custom REST plant-root listing
  - router.py → route composition
"""

from i3x_server.api.mcp.monolithic import router

__all__ = ["router"]
