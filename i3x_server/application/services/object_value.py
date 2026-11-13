"""
Object value retrieval and history service.

Handles current value queries, historical data retrieval, and composition
resolution for i3X model objects.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, Request

from i3x_server.domain.ports.opcua import DataValueDTO, OpcUaClientProtocol, Quality
from i3x_server.errors import i3x_http_error
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.opcua_value import (
    normalize_timestamp,
    now_iso,
    quality_for_payload,
    severity_quality,
    to_json_safe_value,
)
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
                components = self._collect_composition_components(node, max_depth or 0)
                if components:
                    result["components"] = {}
                    for component in components:
                        component_vqt = await self._read_current_value(component)
                        result["components"][component.id] = component_vqt.model_dump()

            return result
        except Exception as exc:
            if isinstance(exc, HTTPException):
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
                components = self._collect_history_source_nodes(node, max_depth or 0)
                if components:
                    result["components"] = {}
                    for component in components:
                        component_history = await self._read_history_values(component, start_dt, end_dt)
                        result["components"][component.id] = [v.model_dump() for v in component_history]

            return result
        except Exception as exc:
            if isinstance(exc, HTTPException):
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
        node = self.model.nodes_by_id.get(element_id)
        if node is None:
            raise i3x_http_error(
                404,
                "NotFound",
                f"Object not found: {element_id}",
                {"element_id": element_id},
            )

        related: list[dict[str, Any]] = []

        def add_related(source_relationship: str, target_id: str) -> None:
            target = self.model.nodes_by_id.get(target_id)
            if target is None:
                return
            metadata: dict[str, Any] | None = None
            if include_metadata:
                metadata = {
                    "sourceTypeId": target.source_type_id,
                    "relationships": target.relationships,
                    "extendedAttributes": target.metadata,
                }
            related.append(
                {
                    "sourceRelationship": source_relationship,
                    "object": {
                        "elementId": target.id,
                        "displayName": target.name,
                        "typeElementId": target.type or target.source_type_id or target.kind,
                        "parentId": target.parent_id,
                        "isComposition": bool(target.is_composition),
                        "isExtended": bool(target.metadata),
                        "metadata": metadata,
                    },
                }
            )

        relationship_map: dict[str, list[str]] = {}
        relationship_map.update(node.relationships)

        child_ids = self.model.children_by_id.get(node.id, [])
        if child_ids:
            relationship_map.setdefault("HasChildren", [])
            relationship_map["HasChildren"].extend(child_ids)

        for rel_name, targets in relationship_map.items():
            if relationship_type is not None and rel_name != relationship_type:
                continue
            for target_id in targets:
                add_related(rel_name, target_id)

        return related

    # Private helpers

    async def _read_current_value(self, node: ModelNode) -> VQT:
        """Read current value from OPC UA for a node."""
        if node.kind != "property":
            return VQT(value=None, quality=Quality.GOOD_NO_DATA, timestamp=self._now_iso())

        source_node_id = self.model.property_to_node.get(node.id) or node.source_node_id
        data_values = await self.opcua_client.read_data_values([source_node_id])
        if not data_values:
            return VQT(value=None, quality=Quality.GOOD_NO_DATA, timestamp=self._now_iso())
        return self._vqt_from_data_value(data_values[0])

    async def _read_history_values(self, node: ModelNode, start_time: datetime, end_time: datetime) -> list[VQT]:
        """Read historical values from OPC UA for a node."""
        if node.kind != "property":
            return []

        source_node_id = self.model.property_to_node.get(node.id) or node.source_node_id
        history_map = await self.opcua_client.read_history_values([source_node_id], start_time, end_time)
        values = history_map.get(source_node_id, [])
        return [self._to_vqt_from_history_value(item) for item in values]

    def _collect_composition_components(self, root: ModelNode, max_depth: int) -> list[ModelNode]:
        """Collect composition child nodes up to max depth."""
        if max_depth == 1:
            return []

        components: list[ModelNode] = []
        queue: deque[tuple[str, int]] = deque([(root.id, 0)])
        visited: set[str] = set()

        while queue:
            node_id, depth = queue.popleft()
            if node_id in visited:
                continue
            visited.add(node_id)

            if max_depth > 0 and depth >= max_depth:
                continue

            current_node = self.model.nodes_by_id.get(node_id)
            if current_node is None:
                continue

            # Composition children only: HasChildren objects are independent (i3X spec
            # IMPLEMENTATION_GUIDE.md:1111/:1924) and must not be read via the parent.
            for child_id in self.model.composition_child_ids(node_id):
                child = self.model.nodes_by_id.get(child_id)
                if child is None:
                    continue
                if child.kind == "property":
                    components.append(child)
                else:
                    queue.append((child.id, depth + 1))

        return components

    def _collect_history_source_nodes(self, root: ModelNode, max_depth: int) -> list[ModelNode]:
        """Collect property nodes for history retrieval.

        ``maxDepth`` counts composition *levels* (i3X spec IMPLEMENTATION_GUIDE.md:1105-1110 and
        :1216): ``1`` = no recursion, ``N > 1`` = up to N levels of ``HasComponent`` descendants,
        ``0`` = unlimited. The root sits at level 0, so ``maxDepth = 2`` also collects the
        grandchildren of the queried composition element - identical to
        ``_collect_composition_components``.
        """
        if root.kind == "property":
            return [root]
        if max_depth == 1:
            return []

        results: list[ModelNode] = []
        visited: set[str] = set()
        queue: list[tuple[str, int]] = [(root.id, 0)]

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

            for child_id in self.model.composition_child_ids(node_id):
                child = self.model.nodes_by_id.get(child_id)
                if child is None:
                    continue
                if child.kind == "property":
                    results.append(child)
                else:
                    queue.append((child.id, depth + 1))

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
        """Current time as an i3X ISO 8601 string (UTC, ``Z`` suffix)."""
        return now_iso()

    def _normalize_quality(self, status_code: int | None) -> Quality:
        return severity_quality(status_code)

    def _normalize_timestamp(self, value: Any) -> str:
        return normalize_timestamp(value)

    def _to_json_safe_value(self, value: Any) -> Any:
        """JSON-safe payload. ``encoding`` fields stay: the service reports the value as read."""
        return to_json_safe_value(value)

    def _vqt_from_data_value(self, data_value: DataValueDTO) -> VQT:
        raw_value = data_value.value.value if data_value.value is not None else None
        safe_value = self._to_json_safe_value(raw_value)
        quality = quality_for_payload(safe_value, self._normalize_quality(data_value.status_code))
        timestamp = self._normalize_timestamp(data_value.source_timestamp or data_value.server_timestamp)
        return VQT(value=safe_value, quality=quality, timestamp=timestamp)

    def _to_vqt_from_history_value(self, data_value: DataValueDTO) -> VQT:
        return self._vqt_from_data_value(data_value)
