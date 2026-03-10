from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

NodeKind = Literal["asset", "property", "action", "eventSource"]


class ModelNode(BaseModel):
    id: str
    name: str
    kind: NodeKind
    type: str | None = None
    children: list[str] = Field(default_factory=list)
    source_node_id: str


class ModelRootResponse(BaseModel):
    items: list[ModelNode]


class ModelChildrenResponse(BaseModel):
    parent_id: str
    children: list[ModelNode]


class DataValueResponse(BaseModel):
    property_id: str
    value: Any
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DataQueryRequest(BaseModel):
    property_ids: list[str]


class DataQueryResponse(BaseModel):
    values: list[DataValueResponse]


class ActionInvokeRequest(BaseModel):
    args: list[Any] = Field(default_factory=list)


class ActionInvokeResponse(BaseModel):
    action_id: str
    result: Any


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorDetail
