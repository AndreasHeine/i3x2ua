"""
Object value retrieval and history service.

Handles current value queries, historical data retrieval, and composition
resolution for i3X model objects.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, Request

from i3x_server.config.settings import history_max_range_days
from i3x_server.domain.ports.opcua import DataValueDTO, OpcUaClientProtocol, Quality
from i3x_server.errors import i3x_http_error
from i3x_server.logging_format import log_upstream_failure
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

            # Collect the whole tree first, then read it in one batched Read service call:
            # composition children cost no extra round trip (the adapter chunks internally).
            components = self._collect_composition_components(node, max_depth or 0) if max_depth != 1 else []
            values_by_source_id = await self._read_values_by_source_id([node, *components])

            result: dict[str, Any] = self._vqt_for_node(node, values_by_source_id).model_dump()
            if components:
                result["components"] = {
                    component.id: self._vqt_for_node(component, values_by_source_id).model_dump()
                    for component in components
                }

            return result
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            message = f"Failed to read current value for {element_id}"
            log_upstream_failure("OpcUaReadError", message, exc)
            raise i3x_http_error(502, "OpcUaReadError", message) from exc

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

            max_days = history_max_range_days()
            if max_days > 0 and end_dt - start_dt > timedelta(days=max_days):
                # Refused before the round-trip: an oversized span asks the upstream for every
                # sample it has stored, which no single request should be able to demand.
                raise i3x_http_error(
                    400,
                    "InvalidArgument",
                    f"History range exceeds the maximum of {max_days} days",
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

            # Read the root and every composition level in one batched HistoryRead call.
            components = self._collect_history_source_nodes(node, max_depth or 0) if max_depth != 1 else []
            history_by_source_id = await self._read_history_by_source_id([node, *components], start_dt, end_dt)
            result: dict[str, Any] = {
                "values": [v.model_dump() for v in self._history_for_node(node, history_by_source_id)]
            }
            if components:
                result["components"] = {
                    component.id: [v.model_dump() for v in self._history_for_node(component, history_by_source_id)]
                    for component in components
                }

            return result
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            message = f"Failed to read history for {element_id}"
            log_upstream_failure("OpcUaHistoryError", message, exc)
            raise i3x_http_error(502, "OpcUaHistoryError", message) from exc

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

    def _source_node_id(self, node: ModelNode) -> str:
        """The OPC UA node that serves this element's value."""
        return self.model.property_to_node.get(node.id) or node.source_node_id

    async def _read_values_by_source_id(self, nodes: list[ModelNode]) -> dict[str, DataValueDTO]:
        """One batched read for every property node, keyed by the OPC UA node that serves it.

        Non-property elements are not read at all, and element ids sharing one OPC UA node are
        requested once.
        """
        source_ids = [self._source_node_id(node) for node in nodes if node.kind == "property"]
        unique_ids = list(dict.fromkeys(source_ids))
        if not unique_ids:
            return {}

        data_values = await self.opcua_client.read_data_values(unique_ids)
        # One result per requested node: a short answer must not pair values with the wrong ids.
        if len(data_values) != len(unique_ids):
            raise RuntimeError(f"OPC UA read returned {len(data_values)} values for {len(unique_ids)} requested nodes")
        return dict(zip(unique_ids, data_values, strict=True))

    async def _read_history_by_source_id(
        self,
        nodes: list[ModelNode],
        start_time: datetime,
        end_time: datetime,
    ) -> dict[str, list[DataValueDTO]]:
        """One batched history read for every property node, keyed by its OPC UA node."""
        source_ids = [self._source_node_id(node) for node in nodes if node.kind == "property"]
        unique_ids = list(dict.fromkeys(source_ids))
        if not unique_ids:
            return {}
        return await self.opcua_client.read_history_values(
            node_ids=unique_ids,
            start_time=start_time,
            end_time=end_time,
        )

    def _vqt_for_node(self, node: ModelNode, values_by_source_id: dict[str, DataValueDTO]) -> VQT:
        """Value of one element from a batched read; non-properties answer ``GoodNoData``."""
        data_value = values_by_source_id.get(self._source_node_id(node)) if node.kind == "property" else None
        if data_value is None:
            return VQT(value=None, quality=Quality.GOOD_NO_DATA, timestamp=self._now_iso())
        return self._vqt_from_data_value(data_value)

    def _history_for_node(
        self,
        node: ModelNode,
        history_by_source_id: dict[str, list[DataValueDTO]],
    ) -> list[VQT]:
        """History of one element from a batched read; non-properties have no history."""
        if node.kind != "property":
            return []
        items = history_by_source_id.get(self._source_node_id(node), [])
        return [self._to_vqt_from_history_value(item) for item in items]

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
