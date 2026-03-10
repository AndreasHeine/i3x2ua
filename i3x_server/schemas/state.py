from dataclasses import dataclass, field

from i3x_server.schemas.i3x import ModelNode


@dataclass(slots=True)
class BuildResult:
    nodes_by_id: dict[str, ModelNode]
    root_ids: list[str]
    children_by_id: dict[str, list[str]]
    property_to_node: dict[str, str]
    action_to_method: dict[str, tuple[str, str]]


@dataclass(slots=True)
class AppState:
    model_cache: BuildResult | None = None
    model_refresh_lock: object = field(default_factory=object)
