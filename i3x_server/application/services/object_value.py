"""
Object value retrieval and history service.

Handles current value queries, historical data retrieval, and composition
resolution for i3X model objects.
"""

from __future__ import annotations

import logging
from base64 import b64encode
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel

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
                components = self._collect_history_source_nodes(node, max_depth or -1)
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
            return VQT(value=None, quality="GoodNoData", timestamp=self._now_iso())

        source_node_id = self.model.property_to_node.get(node.id) or node.source_node_id
        data_values = await self.opcua_client.read_data_values([source_node_id])
        if not data_values:
            return VQT(value=None, quality="GoodNoData", timestamp=self._now_iso())
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

    def _normalize_quality(self, status_code: Any) -> str:
        if status_code is None:
            return "Good"
        is_uncertain = getattr(status_code, "is_uncertain", None)
        if callable(is_uncertain):
            try:
                if bool(is_uncertain()):
                    return "Uncertain"
            except Exception:
                pass
        name = getattr(status_code, "name", "")
        label = str(name) if name else ""
        if "uncertain" in label.lower():
            return "Uncertain"
        is_good = getattr(status_code, "is_good", None)
        if callable(is_good):
            try:
                return "Good" if bool(is_good()) else "Bad"
            except Exception:
                pass
        if "good" in label.lower():
            return "Good"
        if label:
            return "Bad"
        return "Bad"

    def _normalize_timestamp(self, value: Any) -> str:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return str(value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"))
        return self._now_iso()

    def _to_json_safe_value(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, datetime):
            return self._normalize_timestamp(value)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return {"encoding": "base64", "data": b64encode(bytes(value)).decode("ascii")}
        if hasattr(value, "TypeId") and hasattr(value, "Body"):
            body = value.Body
            if body is None:
                return None
            return {
                "TypeId": self._to_json_safe_value(value.TypeId),
                "Body": self._to_json_safe_value(body),
            }
        if isinstance(value, BaseModel):
            return self._to_json_safe_value(value.model_dump(mode="json", by_alias=True))
        if is_dataclass(value):
            return {item.name: self._to_json_safe_value(getattr(value, item.name)) for item in fields(value)}
        if isinstance(value, (list, tuple, set)):
            return [self._to_json_safe_value(item) for item in value]
        if isinstance(value, dict):
            return {str(key): self._to_json_safe_value(item) for key, item in value.items()}
        if hasattr(value, "__dict__") and type(value).__module__ != "builtins":
            return {
                str(key): self._to_json_safe_value(item)
                for key, item in vars(value).items()
                if not key.startswith("_") and not callable(item)
            }
        return str(value)

    def _vqt_from_data_value(self, data_value: Any) -> VQT:
        variant = getattr(data_value, "Value", None)
        raw_value = getattr(variant, "Value", variant)
        status_code = getattr(data_value, "StatusCode", None)
        quality = self._normalize_quality(status_code)
        source_timestamp = getattr(data_value, "SourceTimestamp", None)
        server_timestamp = getattr(data_value, "ServerTimestamp", None)
        timestamp = self._normalize_timestamp(source_timestamp or server_timestamp)
        safe_value = self._to_json_safe_value(raw_value)
        if safe_value is None and quality not in {"Bad", "GoodNoData"}:
            quality = "GoodNoData"
        return VQT(value=safe_value, quality=quality, timestamp=timestamp)

    def _to_vqt_from_history_value(self, data_value: Any) -> VQT:
        return self._vqt_from_data_value(data_value)
