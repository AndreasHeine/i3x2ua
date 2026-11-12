"""ElementId-to-source-node mapping for subscription monitoring.

Pure functions over the built model: which OPC UA source nodes a registered
object should monitor, and the node_id-to-elementId lookup used to label
updates. No service state is involved.
"""

from __future__ import annotations

from collections import deque

from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult


def _build_source_index(model: BuildResult) -> dict[str, ModelNode]:
    """Source-node-id lookup over the whole model.

    Building it is O(all model nodes), so the service caches it per model revision and
    passes it to ``_resolve_object_mappings`` instead of rebuilding it per register call.
    """
    return {item.source_node_id: item for item in model.nodes_by_id.values()}


def _resolve_object_mappings(
    element_id: str,
    max_depth: int,
    model: BuildResult,
    source_index: dict[str, ModelNode],
) -> dict[str, str]:
    """Monitored node_id-to-elementId mappings for one registered object.

    Keys are canonical node ids only; the service adds the case-folded aliases when it
    merges the mappings, mirroring the full-resolution behavior.
    """
    node = model.nodes_by_id.get(element_id)
    if node is None:
        node = source_index.get(element_id)
    if node is None:
        return {element_id: element_id}
    return _collect_property_source_mappings(model, node, max_depth=max_depth)


def _resolve_monitored_node_ids(
    monitored_objects: dict[str, int],
    model: BuildResult,
) -> tuple[set[str], dict[str, str]]:
    source_index = _build_source_index(model)
    node_ids: set[str] = set()
    node_to_element_id: dict[str, str] = {}

    for element_id, max_depth in monitored_objects.items():
        mappings = _resolve_object_mappings(element_id, max_depth, model, source_index)
        node_ids.update(mappings.keys())
        node_to_element_id.update(mappings)
        node_to_element_id.update({key.lower(): value for key, value in mappings.items()})

    return node_ids, node_to_element_id


def _collect_property_source_mappings(
    model: BuildResult,
    root: ModelNode,
    max_depth: int,
) -> dict[str, str]:
    if root.kind == "property":
        return {root.source_node_id: root.id}

    result: dict[str, str] = {}
    depth_limit = max(0, max_depth)
    queue: deque[tuple[str, int]] = deque([(root.id, 0)])

    while queue:
        node_id, depth = queue.popleft()
        node = model.nodes_by_id.get(node_id)
        if node is None:
            continue

        if node.kind == "property":
            result[node.source_node_id] = node.id
            continue

        if depth_limit != 0 and depth >= depth_limit:
            continue

        # Spec: recursion follows HasComponent only; hierarchical children (HasChildren)
        # are independent objects and must never be monitored via the parent.
        for child_id in model.composition_child_ids(node.id):
            queue.append((child_id, depth + 1))

    return result
