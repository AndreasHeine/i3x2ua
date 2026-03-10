from __future__ import annotations

import hashlib

from i3x_server.opcua.client import OpcUaNodeInfo
from i3x_server.schemas.i3x import ModelNode, NodeKind

CLASS_TO_KIND: dict[str, NodeKind] = {
    "Object": "asset",
    "Variable": "property",
    "Method": "action",
}


def stable_i3x_id(node_id: str, kind: NodeKind) -> str:
    digest = hashlib.sha1(node_id.encode("utf-8")).hexdigest()[:16]
    return f"{kind}-{digest}"


def infer_kind(node: OpcUaNodeInfo) -> NodeKind:
    if node.event_notifier:
        return "eventSource"
    return CLASS_TO_KIND.get(node.node_class, "asset")


def map_type(node: OpcUaNodeInfo, kind: NodeKind) -> str | None:
    if kind == "property":
        return node.data_type
    return None


def map_node(node: OpcUaNodeInfo, children: list[str]) -> ModelNode:
    kind = infer_kind(node)
    return ModelNode(
        id=stable_i3x_id(node.node_id, kind),
        name=node.display_name or node.browse_name,
        kind=kind,
        type=map_type(node, kind),
        children=children,
        source_node_id=node.node_id,
    )
