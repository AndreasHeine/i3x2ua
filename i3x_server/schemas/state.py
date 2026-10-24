from dataclasses import dataclass, field

from i3x_server.schemas.i3x import ModelNode


@dataclass(slots=True)
class _ModelAccessIndex:
    revision: str
    ordered_ids: tuple[str, ...]
    namespaces: dict[str, list[str]] = field(default_factory=dict)
    roles: dict[str, list[str]] = field(default_factory=dict)
    name_grams: dict[str, set[str]] = field(default_factory=dict)
    children_by_plane: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)
    paths: dict[tuple[str, ...], list[str]] | None = None
    type_context_token: object | None = None
    direct_instances: dict[str, tuple[str, ...]] = field(default_factory=dict)
    subtype_instances: dict[str, tuple[str, ...]] = field(default_factory=dict)


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
    _reverse_parent_index_cache: tuple[tuple[int, int], dict[str, str]] | None = field(
        default=None, init=False, repr=False, compare=False
    )


@dataclass(slots=True)
class AppState:
    model_cache: BuildResult | None = None
    model_refresh_lock: object = field(default_factory=object)
