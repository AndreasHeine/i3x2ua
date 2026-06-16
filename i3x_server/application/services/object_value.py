"""
Object value retrieval and history service.

Handles current value queries, historical data retrieval, and composition
resolution for i3X model objects.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import Request

from i3x_server.domain.ports.opcua import OpcUaClientProtocol
from i3x_server.errors import i3x_http_error
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult

logger = logging.getLogger(__name__)


class VQT:
    """Value-Quality-Timestamp tuple."""

    def __init__(self, value: Any, quality: str, timestamp: str):
        self.value = value
        self.quality = quality
        self.timestamp = timestamp

    def model_dump(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "quality": self.quality,
            "timestamp": self.timestamp,
        }


class ObjectValueService:
    """Orchestrates object value queries and history retrieval."""

    def __init__(
        self,
        opcua_client: OpcUaClientProtocol,
        model: BuildResult,
        request: Request | None = None,
    ):
        """Initialize service with dependencies.

        Args:
            opcua_client: OPC UA protocol client
            model: Pre-built model structure
            request: Optional FastAPI request for caching
        """
        self.opcua_client = opcua_client
        self.model = model
        self.request = request

    async def get_current_value(
        self,
        element_id: str,
        max_depth: int | None = 1,
    ) -> dict[str, Any]:
        """Retrieve current value for an object.

        Args:
            element_id: The element ID to query
            max_depth: Maximum composition depth

        Returns:
            Value with quality and timestamp, optionally with component values
        """
        try:
            # Find the model node
            node = self.model.nodes_by_id.get(element_id)
            if node is None:
                raise i3x_http_error(
                    404,
                    "NotFound",
                    f"Object not found: {element_id}",
                    {"element_id": element_id},
                )

            # Read current value from OPC UA
            value_vqt = await self._read_current_value(node)
            result: dict[str, Any] = value_vqt.model_dump()

            # If composition, collect component values
            if max_depth != 1:
                components = self._collect_composition_components(node, max_depth or -1)
                if components:
                    result["components"] = {}
                    for component in components:
                        component_vqt = await self._read_current_value(component)
                        result["components"][component.id] = component_vqt.model_dump()

            return result
        except Exception as exc:
            if isinstance(exc, type(i3x_http_error(400, "", ""))):
                raise
            raise i3x_http_error(
                502,
                "OpcUaReadError",
                f"Failed to read current value for {element_id}",
                {"cause": str(exc)},
            ) from exc

    async def get_history(
        self,
        element_id: str,
        start_time: str,
        end_time: str,
        max_depth: int | None = 1,
    ) -> dict[str, Any]:
        """Retrieve historical values for an object.

        Args:
            element_id: The element ID to query
            start_time: Start time (ISO 8601)
            end_time: End time (ISO 8601)
            max_depth: Maximum composition depth

        Returns:
            Historical values with quality and timestamps
        """
        try:
            # Parse and validate time range
            start_dt = self._parse_iso_datetime(start_time, "startTime")
            end_dt = self._parse_iso_datetime(end_time, "endTime")

            if start_dt > end_dt:
                raise i3x_http_error(
                    400,
                    "InvalidArgument",
                    "startTime must be less than or equal to endTime",
                    {"startTime": start_time, "endTime": end_time},
                )

            # Find model node
            node = self.model.nodes_by_id.get(element_id)
            if node is None:
                raise i3x_http_error(
                    404,
                    "NotFound",
                    f"Object not found: {element_id}",
                    {"element_id": element_id},
                )

            # Read historical values
            history_values = await self._read_history_values(node, start_dt, end_dt)
            result: dict[str, Any] = {"values": [v.model_dump() for v in history_values]}

            # If composition, collect component histories
            if max_depth != 1:
                components = self._collect_history_source_nodes(node, max_depth or -1)
                if components:
                    result["components"] = {}
                    for component in components:
                        component_history = await self._read_history_values(component, start_dt, end_dt)
                        result["components"][component.id] = [v.model_dump() for v in component_history]

            return result
        except Exception as exc:
            if isinstance(exc, type(i3x_http_error(400, "", ""))):
                raise
            raise i3x_http_error(
                502,
                "OpcUaHistoryError",
                f"Failed to read history for {element_id}",
                {"cause": str(exc)},
            ) from exc

    async def get_related_objects(
        self,
        element_id: str,
        relationship_type: str | None = None,
        include_metadata: bool = False,
    ) -> list[dict[str, Any]]:
        """Retrieve objects related by composition or reference.

        Args:
            element_id: The element ID to query
            relationship_type: Optional relationship type filter
            include_metadata: Whether to include full metadata

        Returns:
            List of related objects
        """
        raise NotImplementedError("Related objects retrieval in progress")

    # Private helpers

    async def _read_current_value(self, node: ModelNode) -> VQT:
        """Read current value from OPC UA for a node."""
        # Placeholder for OPC UA read implementation
        return VQT(value=None, quality="GoodNoData", timestamp=self._now_iso())

    async def _read_history_values(self, node: ModelNode, start_time: datetime, end_time: datetime) -> list[VQT]:
        """Read historical values from OPC UA for a node."""
        # Placeholder for OPC UA history read implementation
        return []

    def _collect_composition_components(self, root: ModelNode, max_depth: int) -> list[ModelNode]:
        """Collect composition child nodes up to max depth."""
        if max_depth == 1:
            return []

        components: list[ModelNode] = []
        queue: list[tuple[str, int]] = [(root.id, 0)]
        visited: set[str] = set()

        while queue:
            node_id, depth = queue.pop(0)
            if node_id in visited:
                continue
            visited.add(node_id)

            if max_depth > 0 and depth >= max_depth:
                continue

            current_node = self.model.nodes_by_id.get(node_id)
            if current_node is None:
                continue

            # Get composition children (properties)
            for child_id in self.model.children_by_id.get(node_id, []):
                child = self.model.nodes_by_id.get(child_id)
                if child is None:
                    continue
                if child.kind == "property":
                    components.append(child)
                else:
                    queue.append((child.id, depth + 1))

        return components

    def _collect_history_source_nodes(self, root: ModelNode, max_depth: int) -> list[ModelNode]:
        """Collect property nodes for history retrieval."""
        if root.kind == "property":
            return [root]

        results: list[ModelNode] = []
        visited: set[str] = set()
        queue: list[tuple[str, int]] = [(root.id, 1)]

        while queue:
            current_id, depth = queue.pop(0)
            if current_id in visited:
                continue
            visited.add(current_id)

            current = self.model.nodes_by_id.get(current_id)
            if current is None:
                continue
            if current.kind == "property":
                results.append(current)

            if max_depth != 0 and depth >= max_depth:
                continue

            current_node = self.model.nodes_by_id.get(current_id)
            if current_node is None:
                continue

            for child_id in self.model.children_by_id.get(current_id, []):
                queue.append((child_id, depth + 1))

        return results

    def _parse_iso_datetime(self, value: str, field_name: str) -> datetime:
        """Parse ISO 8601 datetime string."""
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise i3x_http_error(
                400,
                "InvalidArgument",
                f"Invalid ISO 8601 timestamp for '{field_name}'",
                {"field": field_name, "value": value},
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _now_iso(self) -> str:
        """Get current time as ISO 8601 string."""
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
