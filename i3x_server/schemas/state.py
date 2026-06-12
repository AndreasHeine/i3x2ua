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


@dataclass(slots=True)
class AppState:
    model_cache: BuildResult | None = None
    model_refresh_lock: object = field(default_factory=object)
