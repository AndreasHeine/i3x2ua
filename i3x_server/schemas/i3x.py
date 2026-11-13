"""The built i3X model node and the planes that reference it."""

from typing import Any, Literal

from pydantic import BaseModel, Field

NodeKind = Literal["asset", "property", "action", "eventSource"]
SemanticRole = Literal["asset", "group", "component", "datapoint", "property", "unknown"]
MappingConfidence = Literal["high", "medium", "low"]


class ModelNode(BaseModel):
    id: str
    name: str
    kind: NodeKind
    type: str | None = None
    children: list[str] = Field(default_factory=list)
    source_node_id: str
    source_type_id: str | None = None
    parent_id: str | None = None
    is_composition: bool = False
    semantic_role: SemanticRole = "unknown"
    mapping_confidence: MappingConfidence = "medium"
    relationships: dict[str, list[str]] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
