"""
MCP (Model Context Protocol) orchestration service.

Handles tool invocation, prompt execution, resource discovery and delivery,
and MCP initialization.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import Request

from i3x_server.errors import i3x_http_error
from i3x_server.prompts.registry import PromptRegistry

logger = logging.getLogger(__name__)


class McpService:
    """Orchestrates MCP protocol operations."""

    def __init__(
        self,
        request: Request,
        prompt_registry: PromptRegistry | None = None,
    ):
        """Initialize service with dependencies.

        Args:
            request: FastAPI request for accessing app state
            prompt_registry: Optional prompt registry
        """
        self.request = request
        self.prompt_registry = prompt_registry

    def get_tools(self) -> dict[str, Any]:
        """Retrieve registered tools.

        Returns:
            Dictionary mapping tool name to tool descriptor
        """
        tools = getattr(self.request.app.state, "mcp_tools", {})
        if not isinstance(tools, Mapping):
            return {}

        result = {}
        for name, tool in tools.items():
            if not hasattr(tool, "to_dict"):
                continue
            tool_dict = tool.to_dict()
            if not isinstance(tool_dict, dict):
                continue
            tool_dict["inputSchema"] = tool_dict.get("input_schema", {})
            tool_dict["pathParameters"] = tool_dict.get("path_parameters", [])
            tool_dict["queryParameters"] = tool_dict.get("query_parameters", [])
            tool_dict["bodyRequired"] = tool_dict.get("body_required", False)
            result[name] = tool_dict

        return result

    async def invoke_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Invoke a registered tool.

        Args:
            tool_name: Name of tool to invoke
            arguments: Tool arguments

        Returns:
            Tool result
        """
        from i3x_server.mcp import invoke_mcp_tool

        tools = getattr(self.request.app.state, "mcp_tools", {})
        if not isinstance(tools, Mapping) or tool_name not in tools:
            raise i3x_http_error(
                400,
                "Bad Request",
                f"Unknown tool {tool_name}",
            )

        tool = tools[tool_name]
        payload = await invoke_mcp_tool(self.request, tool, arguments)
        if isinstance(payload, dict):
            return payload
        raise i3x_http_error(500, "Internal Error", "Invalid MCP tool response")

    def get_prompts(self) -> list[dict[str, Any]]:
        """Retrieve registered prompts.

        Returns:
            List of prompt metadata
        """
        from i3x_server.prompts.api import list_prompt_metadata

        return list_prompt_metadata(self.prompt_registry)

    async def get_prompt(self, name: str) -> dict[str, Any]:
        """Retrieve a prompt definition.

        Args:
            name: Prompt name

        Returns:
            Prompt definition
        """
        from i3x_server.prompts.api import get_prompt

        return get_prompt(self.prompt_registry, name)

    async def execute_prompt(
        self,
        name: str,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a prompt template.

        Args:
            name: Prompt name
            parameters: Template parameters

        Returns:
            Rendered prompt result
        """
        from i3x_server.prompts.api import execute_prompt

        return execute_prompt(self.prompt_registry, name, parameters)

    def get_resources(self) -> list[dict[str, Any]]:
        """Retrieve resource catalog.

        Returns:
            List of resource descriptors
        """
        resources: list[dict[str, Any]] = [
            {
                "uri": "i3x://openapi",
                "name": "OpenAPI specification",
                "description": "Server OpenAPI JSON document",
                "mimeType": "application/json",
            },
            {
                "uri": "i3x://mcp-overrides",
                "name": "MCP overrides",
                "description": "Runtime MCP tool, prompt, and feature overrides",
                "mimeType": "application/json",
            },
            {
                "uri": "i3x://docs/quick-reference",
                "name": "Quick reference",
                "description": "Server quick reference documentation",
                "mimeType": "text/markdown",
            },
        ]

        # Add prompts as resources
        for prompt in self.get_prompts():
            resources.append(
                {
                    "uri": f"i3x://prompts/{prompt['name']}",
                    "name": f"Prompt: {prompt['name']}",
                    "description": prompt.get("description", ""),
                    "mimeType": "application/json",
                }
            )

        return resources

    async def read_resource(self, uri: str) -> dict[str, Any]:
        """Read resource content.

        Args:
            uri: Resource URI

        Returns:
            Resource with content
        """
        project_root = self._get_project_root()

        if uri == "i3x://openapi":
            return {
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(self.request.app.openapi(), ensure_ascii=False),
            }

        if uri == "i3x://mcp-overrides":
            path = project_root / "overrides" / "mcp_overrides.json"
            if not path.exists():
                raise i3x_http_error(404, "Not Found", f"Unknown resource {uri}")
            return {
                "uri": uri,
                "mimeType": "application/json",
                "text": path.read_text(encoding="utf-8"),
            }

        if uri == "i3x://docs/quick-reference":
            path = project_root / "docs" / "QUICK_REFERENCE.md"
            if not path.exists():
                raise i3x_http_error(404, "Not Found", f"Unknown resource {uri}")
            return {
                "uri": uri,
                "mimeType": "text/markdown",
                "text": path.read_text(encoding="utf-8"),
            }

        prompt_prefix = "i3x://prompts/"
        if uri.startswith(prompt_prefix):
            prompt_name = uri.removeprefix(prompt_prefix)
            prompt = await self.get_prompt(prompt_name)
            return {
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(prompt, ensure_ascii=False),
            }

        raise i3x_http_error(404, "Not Found", f"Unknown resource {uri}")

    def get_roots(self) -> list[dict[str, Any]]:
        """Retrieve root model node list.

        Returns:
            List of root descriptors (uri and name)
        """
        model_cache = getattr(self.request.app.state, "model_cache", None)
        if model_cache is None:
            return []

        root_ids = getattr(model_cache, "root_ids", [])
        nodes_by_id = getattr(model_cache, "nodes_by_id", {})
        roots: list[dict[str, Any]] = []

        for root_id in root_ids:
            node = nodes_by_id.get(root_id)
            display_name = getattr(node, "name", str(root_id)) if node is not None else str(root_id)
            roots.append(
                {
                    "uri": f"i3x://roots/{root_id}",
                    "name": display_name,
                }
            )

        return roots

    # Private helpers

    def _get_project_root(self) -> Path:
        """Get project root directory."""
        return Path(__file__).resolve().parents[3]
