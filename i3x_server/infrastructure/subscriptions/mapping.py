"""ElementId-to-source-node mapping for subscription monitoring.

Pure functions over the built model: which OPC UA source nodes a registered
object should monitor, and the node_id-to-elementId lookup used to label
updates. No service state is involved. A ``max_nodes`` budget makes an oversized
subtree a rejection instead of a silent truncation: monitoring thousands of
nodes per object would multiply into unbounded upstream load. ``max_depth_limit``
bounds the traversal depth itself, so a request for unlimited depth cannot walk
an arbitrarily deep chain.
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


def _raise_depth_exceeded(element_id: str, depth: int, max_depth: int) -> None:
    raise AdmissionLimitError(
        400,
        "CompositionDepthLimit",
        "Composition depth limit exceeded",
        {"elementId": element_id, "compositionDepth": depth, "maxDepth": max_depth},
    )


def _composition_depth_limit(max_depth: int, max_depth_limit: int | None) -> tuple[int, bool]:
    """Effective composition level limit and whether the gateway bound cuts the request short.

    ``max_depth`` is the caller's request (``0`` = unlimited), ``max_depth_limit`` the gateway's
    own bound (``None``/``0`` = unbounded). The second value says the traversal stopped below
    what the caller asked for, so an incomplete mapping is rejected rather than committed.
    """
    requested = max_depth if max_depth > 0 else None
    bound = max_depth_limit if max_depth_limit is not None and max_depth_limit > 0 else None
    if requested is None:
        return (bound, bound is not None) if bound is not None else (0, False)
    if bound is None:
        return requested, False
    return min(requested, bound), requested > bound


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
    max_depth_limit: int | None = None,
) -> dict[str, str]:
    """Monitored node_id-to-elementId mappings for one registered object.

    Keys are canonical node ids only; the service adds the case-folded aliases when it
    merges the mappings, mirroring the full-resolution behavior. ``max_nodes`` bounds the
    subtree of this single object (``None`` = no gateway-side budget), ``max_depth_limit``
    the composition depth beneath it.
    """
    node = model.nodes_by_id.get(element_id)
    if node is None:
        node = source_index.get(element_id)
    if node is None:
        if max_nodes is not None and max_nodes < 1:
            _raise_budget_exceeded(element_id, 1, max_nodes)
        return {element_id: element_id}
    return _collect_property_source_mappings(
        model,
        node,
        max_depth=max_depth,
        max_nodes=max_nodes,
        max_depth_limit=max_depth_limit,
    )


def _resolve_monitored_node_ids(
    monitored_objects: dict[str, int],
    model: BuildResult,
    max_nodes: int | None = None,
    max_depth_limit: int | None = None,
) -> tuple[set[str], dict[str, str]]:
    """Node set and label lookup for all monitored objects, under an optional shared budget."""
    source_index = _build_source_index(model)
    node_ids: set[str] = set()
    node_to_element_id: dict[str, str] = {}

    for element_id, max_depth in monitored_objects.items():
        mappings = _resolve_object_mappings(
            element_id,
            max_depth,
            model,
            source_index,
            max_nodes=max_nodes,
            max_depth_limit=max_depth_limit,
        )
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
    max_depth_limit: int | None = None,
) -> dict[str, str]:
    """Composition descendants of ``root`` as monitored node_id-to-elementId mappings.

    ``max_nodes`` bounds the subtree of this single object (``None`` = no gateway-side budget)
    and ``max_depth_limit`` the composition depth beneath it (``None``/``0`` = unbounded).
    ``visited`` makes ``HasComponent``/``ComponentOf`` cycles terminate: without it an
    unlimited request (``maxDepth=0``) re-enqueued cyclic nodes forever.
    """
    if root.kind == "property":
        return {root.source_node_id: root.id}

    result: dict[str, str] = {}
    depth_limit, limited_by_gateway = _composition_depth_limit(max_depth, max_depth_limit)
    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(root.id, 0)])

    while queue:
        node_id, depth = queue.popleft()
        if node_id in visited:
            # A shared or cyclic descendant is collected once; re-enqueueing it would repeat work
            # forever on a cycle.
            continue
        visited.add(node_id)

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
            # Children already visited are not lost; only genuinely unvisited descendants mean
            # the gateway bound cut the requested subtree short.
            if limited_by_gateway and any(child_id not in visited for child_id in model.composition_child_ids(node.id)):
                _raise_depth_exceeded(root.id, depth, depth_limit)
            continue

        # Spec: recursion follows HasComponent only; hierarchical children (HasChildren)
        # are independent objects and must never be monitored via the parent.
        for child_id in model.composition_child_ids(node.id):
            queue.append((child_id, depth + 1))

    return result
