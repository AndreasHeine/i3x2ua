"""
MCP protocol endpoint package.

Protocol surfaces live in focused modules:
  - jsonrpc.py → JSON-RPC single-message request/response handling
  - tools.py → tools/list, tools/call
  - prompts.py → prompts/list, prompts/get, prompts/execute
  - resources.py → resources/list, resources/read
  - roots.py → custom REST plant-root listing
  - router.py → route composition

For private internals, tests and monkeypatching must target these source
modules, not this package's re-exports.
"""

from i3x_server.api.mcp.jsonrpc import publish_mcp_notification, shutdown_mcp_transport
from i3x_server.api.mcp.router import router

__all__ = ["publish_mcp_notification", "router", "shutdown_mcp_transport"]
