"""MCP adapter implementation.

The package-level imports preserve the former ``i3x_server.mcp`` API while
the implementation lives in responsibility-specific modules.
"""

from i3x_server.mcp.invocation import (
    _InternalASGIResponse,
    _invoke_container_values,
    _invoke_internal_asgi,
    _paginate_mcp_history,
    _paginate_mcp_related,
    _payload_to_response,
    _safe_api_prefix,
    _safe_path_parameter_value,
    _safe_request_path,
    _trace_log_fields,
    call_mcp_tool,
    get_api_prefix,
    init_mcp_metrics,
    invoke_mcp_tool,
)
from i3x_server.mcp.tool_definitions import (
    DEFAULT_MCP_OVERRIDES_PATH,
    DEFAULT_MCP_OVERRIDES_SCHEMA_PATH,
    McpToolDefinition,
    _resolve_override_path,
    _resolve_request_body_schema,
    _resolve_schema,
    build_mcp_tools,
    load_feature_overrides,
    load_overrides,
    load_prompt_overrides,
    load_tool_overrides,
)

__all__ = [
    "DEFAULT_MCP_OVERRIDES_PATH",
    "DEFAULT_MCP_OVERRIDES_SCHEMA_PATH",
    "McpToolDefinition",
    "_InternalASGIResponse",
    "_invoke_container_values",
    "_invoke_internal_asgi",
    "_paginate_mcp_history",
    "_paginate_mcp_related",
    "_payload_to_response",
    "_resolve_override_path",
    "_resolve_request_body_schema",
    "_resolve_schema",
    "_safe_api_prefix",
    "_safe_path_parameter_value",
    "_safe_request_path",
    "_trace_log_fields",
    "build_mcp_tools",
    "call_mcp_tool",
    "get_api_prefix",
    "init_mcp_metrics",
    "invoke_mcp_tool",
    "load_feature_overrides",
    "load_overrides",
    "load_prompt_overrides",
    "load_tool_overrides",
]
