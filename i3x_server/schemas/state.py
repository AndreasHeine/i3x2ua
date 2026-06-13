from dataclasses import dataclass, field

from i3x_server.schemas.i3x import ModelNode


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


@dataclass(slots=True)
class AppState:
    model_cache: BuildResult | None = None
    model_refresh_lock: object = field(default_factory=object)
