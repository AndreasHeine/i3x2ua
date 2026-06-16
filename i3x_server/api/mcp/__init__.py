"""
MCP protocol endpoint package.

This module maintains backward compatibility during the refactoring of the
monolithic mcp.py into protocol-surface-specific modules. Currently, it
re-exports the router from the single-file implementation.

During Phase 3+, this package will gradually absorb protocol surfaces into
focused modules:
  - jsonrpc.py → JSON-RPC request/response/batch handling
  - tools.py → tools/list, tools/call
  - prompts.py → prompts/list, prompts/get, prompts/execute
  - resources.py → resources/list, resources/read
  - roots.py → roots/list
  - sse.py → SSE discovery endpoint
  - router.py → route composition
"""

# Phase 3 placeholder: import from monolithic implementation
# Future phases will introduce protocol-surface modules here
from i3x_server.api.mcp.monolithic import router

__all__ = ["router"]
