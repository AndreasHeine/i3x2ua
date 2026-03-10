from __future__ import annotations

from i3x_server.model.mapper import map_node, stable_i3x_id
from i3x_server.opcua.client import OpcUaClientProtocol, OpcUaNodeInfo
from i3x_server.schemas.i3x import ModelNode, NodeKind
from i3x_server.schemas.state import BuildResult


class ModelBuilder:
    def __init__(self, opcua_client: OpcUaClientProtocol) -> None:
        self._opcua_client = opcua_client

    async def build(self) -> BuildResult:
        opc_nodes = await self._opcua_client.browse_tree()
        by_source_node = {node.node_id: node for node in opc_nodes}

        child_sources_by_parent: dict[str, list[str]] = {}
        for node in opc_nodes:
            if node.parent_node_id is None:
                continue
            child_sources_by_parent.setdefault(node.parent_node_id, []).append(node.node_id)

        nodes_by_id: dict[str, ModelNode] = {}
        children_by_id: dict[str, list[str]] = {}
        root_ids: list[str] = []
        property_to_node: dict[str, str] = {}
        action_to_method: dict[str, tuple[str, str]] = {}

        for source_id, opc_node in by_source_node.items():
            child_sources = child_sources_by_parent.get(source_id, [])
            child_ids = [
                stable_i3x_id(by_source_node[c].node_id, _kind_for_node(by_source_node[c]))
                for c in child_sources
            ]
            mapped = map_node(opc_node, child_ids)
            nodes_by_id[mapped.id] = mapped
            children_by_id[mapped.id] = child_ids

            if opc_node.parent_node_id is None:
                root_ids.append(mapped.id)

            if mapped.kind == "property":
                property_to_node[mapped.id] = opc_node.node_id

            if mapped.kind == "action":
                parent = opc_node.parent_node_id
                if parent is not None:
                    action_to_method[mapped.id] = (parent, opc_node.node_id)

        return BuildResult(
            nodes_by_id={key: value for key, value in nodes_by_id.items()},
            root_ids=root_ids,
            children_by_id=children_by_id,
            property_to_node=property_to_node,
            action_to_method=action_to_method,
        )


def _kind_for_node(node: OpcUaNodeInfo) -> NodeKind:
    event_notifier = node.event_notifier
    node_class = node.node_class
    if event_notifier:
        return "eventSource"
    if node_class == "Variable":
        return "property"
    if node_class == "Method":
        return "action"
    return "asset"
