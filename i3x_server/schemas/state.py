"""Build state: the ``BuildResult`` model and the caches it owns."""

from dataclasses import dataclass, field
from typing import Any

from i3x_server.schemas.i3x import ModelNode


@dataclass(slots=True)
class _ModelAccessIndex:
    revision: str
    ordered_ids: tuple[str, ...]
    namespaces: dict[str, list[str]] = field(default_factory=dict)
    roles: dict[str, list[str]] = field(default_factory=dict)
    name_grams: dict[str, set[str]] = field(default_factory=dict)
    children_by_plane: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)
    path_children: dict[str | None, dict[str, tuple[str, ...]]] | None = None
    type_context_token: object | None = None
    direct_instances: dict[str, tuple[str, ...]] = field(default_factory=dict)
    subtype_instances: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(slots=True)
class _McpToolIndex:
    """Derived lookup tables for the MCP model tools, built once per model revision.

    ``searchable_by_id`` keeps the casefolded search fields per element (substring
    matching stays field-by-field), and the adjacency maps hold every relationship as
    ``(target, relationship type, plane)`` so path tracing does not rebuild the graph.
    """

    revision: str
    searchable_by_id: dict[str, tuple[str, ...]] = field(default_factory=dict)
    relationship_names_by_id: dict[str, tuple[str, ...]] = field(default_factory=dict)
    outgoing_by_id: dict[str, tuple[tuple[str, str, str], ...]] = field(default_factory=dict)
    incoming_by_id: dict[str, tuple[tuple[str, str, str], ...]] = field(default_factory=dict)


@dataclass(slots=True)
class BuildResult:
    nodes_by_id: dict[str, ModelNode]
    root_ids: list[str]
    children_by_id: dict[str, list[str]]
    instances_by_type_id: dict[str, list[str]]
    property_to_node: dict[str, str]
    action_to_method: dict[str, tuple[str, str]]
    parent_by_id: dict[str, str] = field(default_factory=dict)
    node_id_by_name: dict[str, str] = field(default_factory=dict)
    node_id_by_type: dict[str, str] = field(default_factory=dict)
    hierarchy_children_by_id: dict[str, list[str]] = field(default_factory=dict)
    composition_children_by_id: dict[str, list[str]] = field(default_factory=dict)
    graph_related_by_id: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    relationships_by_id: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    hierarchy_parent_by_id: dict[str, str] = field(default_factory=dict)
    composition_parent_by_id: dict[str, str] = field(default_factory=dict)
    graph_relationship_names: set[str] = field(default_factory=set)
    graph_relationship_namespace_by_id: dict[str, str] = field(default_factory=dict)
    graph_relationship_display_name_by_id: dict[str, str] = field(default_factory=dict)
    # Reverse relationship for each graph relationship, taken from the address space when the
    # source models the inverse itself (natural names such as `MonitoredBy`); synthesised
    # `inverseOf_<name>` only when the address space defines none (spec IMPLEMENTATION_GUIDE.md:397).
    graph_relationship_inverse_by_id: dict[str, str] = field(default_factory=dict)
    semantic_role_by_id: dict[str, str] = field(default_factory=dict)
    mapping_confidence_by_id: dict[str, str] = field(default_factory=dict)
    parent_id_by_id: dict[str, str | None] = field(default_factory=dict)
    applied_profile_ids_by_id: dict[str, list[str]] = field(default_factory=dict)
    namespace_uri_by_id: dict[str, str | None] = field(default_factory=dict)
    element_id_by_node_id: dict[str, str] = field(default_factory=dict)
    element_id_by_expanded_node_id: dict[str, str] = field(default_factory=dict)
    namespace_uri_by_index: dict[int, str] = field(default_factory=dict)
    browse_duration_s: float = 0.0
    map_duration_s: float = 0.0
    total_duration_s: float = 0.0
    build_completed_at_utc: str | None = None
    revision: str = ""
    _model_access_index_cache: _ModelAccessIndex | None = field(default=None, init=False, repr=False, compare=False)
    # Derived MCP tool lookup tables (search fields + relationship adjacency), rebuilt only
    # when the model revision changes; dataclasses.replace() resets it.
    _mcp_tool_index_cache: _McpToolIndex | None = field(default=None, init=False, repr=False, compare=False)
    _reverse_parent_index_cache: tuple[tuple[str, int, int], dict[str, str]] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    # Response for /model/metrics, computed once per snapshot; dataclasses.replace() resets it.
    _model_metrics_cache: Any = field(default=None, init=False, repr=False, compare=False)

    def composition_child_ids(self, node_id: str) -> list[str]:
        """Composition (``HasComponent``) children of a node.

        Recursion for value/history/monitoring follows only this plane: hierarchical children
        (``HasChildren``) are independent objects and must never be collected via the parent.
        Falls back to property-kind children of ``children_by_id`` when the composition plane was
        not populated (partial/older model states).
        """
        children = self.composition_children_by_id.get(node_id)
        if children is not None:
            return [child_id for child_id in children if isinstance(child_id, str)]
        return [
            child_id
            for child_id in self.children_by_id.get(node_id, [])
            if isinstance(child_id, str)
            and self.nodes_by_id.get(child_id) is not None
            and self.nodes_by_id[child_id].kind == "property"
        ]
