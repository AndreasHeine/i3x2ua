"""ElementId-to-source-node mapping for subscription monitoring.

Pure functions over the built model: which OPC UA source nodes a registered
object should monitor, and the node_id-to-elementId lookup used to label
updates. No service state is involved. A ``max_nodes`` budget makes an oversized
subtree a rejection instead of a silent truncation: monitoring thousands of
nodes per object would multiply into unbounded upstream load.
"""

from __future__ import annotations

from collections import deque

from i3x_server.domain.errors import AdmissionLimitError
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult


def _raise_budget_exceeded(element_id: str, node_count: int, max_nodes: int) -> None:
    raise AdmissionLimitError(
        400,
        "MonitoredItemBudget",
        "Monitored item budget exceeded",
        {"elementId": element_id, "monitoredNodes": node_count, "maxNodes": max_nodes},
    )


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
    max_nodes: int | None = None,
) -> dict[str, str]:
    """Monitored node_id-to-elementId mappings for one registered object.

    Keys are canonical node ids only; the service adds the case-folded aliases when it
    merges the mappings, mirroring the full-resolution behavior. ``max_nodes`` bounds the
    subtree of this single object (``None`` = no gateway-side budget).
    """
    node = model.nodes_by_id.get(element_id)
    if node is None:
        node = source_index.get(element_id)
    if node is None:
        if max_nodes is not None and max_nodes < 1:
            _raise_budget_exceeded(element_id, 1, max_nodes)
        return {element_id: element_id}
    return _collect_property_source_mappings(model, node, max_depth=max_depth, max_nodes=max_nodes)


def _resolve_monitored_node_ids(
    monitored_objects: dict[str, int],
    model: BuildResult,
    max_nodes: int | None = None,
) -> tuple[set[str], dict[str, str]]:
    """Node set and label lookup for all monitored objects, under an optional shared budget."""
    source_index = _build_source_index(model)
    node_ids: set[str] = set()
    node_to_element_id: dict[str, str] = {}

    for element_id, max_depth in monitored_objects.items():
        mappings = _resolve_object_mappings(element_id, max_depth, model, source_index, max_nodes=max_nodes)
        # The budget counts the *union* of monitored nodes: subtrees that share a node must not
        # double-count it, and a total above the cap is a rejection, not a truncation.
        union = node_ids | set(mappings.keys())
        if max_nodes is not None and len(union) > max_nodes:
            _raise_budget_exceeded(element_id, len(union), max_nodes)
        node_ids = union
        node_to_element_id.update(mappings)
        node_to_element_id.update({key.lower(): value for key, value in mappings.items()})

    return node_ids, node_to_element_id


def _collect_property_source_mappings(
    model: BuildResult,
    root: ModelNode,
    max_depth: int,
    max_nodes: int | None = None,
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
            if max_nodes is not None and len(result) > max_nodes:
                # Reject rather than truncate: a partial mapping would report monitoring that
                # never happens.
                _raise_budget_exceeded(root.id, len(result), max_nodes)
            continue

        if depth_limit != 0 and depth >= depth_limit:
            continue

        # Spec: recursion follows HasComponent only; hierarchical children (HasChildren)
        # are independent objects and must never be monitored via the parent.
        for child_id in model.composition_child_ids(node.id):
            queue.append((child_id, depth + 1))

    return result
