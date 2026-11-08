"""ElementId-to-source-node mapping for subscription monitoring.

Pure functions over the built model: which OPC UA source nodes a registered
object should monitor, and the node_id-to-elementId lookup used to label
updates. No service state is involved.
"""

from __future__ import annotations

from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult


def _resolve_monitored_node_ids(
    monitored_objects: dict[str, int],
    model: BuildResult,
) -> tuple[set[str], dict[str, str]]:
    source_index = {item.source_node_id: item for item in model.nodes_by_id.values()}
    node_ids: set[str] = set()
    node_to_element_id: dict[str, str] = {}

    for element_id, max_depth in monitored_objects.items():
        node = model.nodes_by_id.get(element_id)
        if node is None:
            node = source_index.get(element_id)
        if node is None:
            node_ids.add(element_id)
            node_to_element_id[element_id] = element_id
            node_to_element_id[element_id.lower()] = element_id
            continue

        mappings = _collect_property_source_mappings(model, node, max_depth=max_depth)
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
    queue: list[tuple[str, int]] = [(root.id, 0)]

    while queue:
        node_id, depth = queue.pop(0)
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
