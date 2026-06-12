from __future__ import annotations

import logging
from time import perf_counter

from i3x_server.model.mapper import map_node, stable_i3x_id
from i3x_server.opcua.client import OpcUaClientProtocol, OpcUaNodeInfo
from i3x_server.schemas.i3x import ModelNode, NodeKind
from i3x_server.schemas.state import BuildResult

logger = logging.getLogger(__name__)


class ModelBuilder:
    def __init__(self, opcua_client: OpcUaClientProtocol) -> None:
        self._opcua_client = opcua_client

    async def build(self) -> BuildResult:
        started = perf_counter()
        browse_started = perf_counter()
        opc_nodes = await self._opcua_client.browse_tree()
        browse_duration_s = perf_counter() - browse_started
        map_started = perf_counter()
        by_source_node = {node.node_id: node for node in opc_nodes}

        child_sources_by_parent: dict[str, list[str]] = {}
        for node in opc_nodes:
            if node.parent_node_id is None:
                continue
            child_sources_by_parent.setdefault(node.parent_node_id, []).append(node.node_id)

        nodes_by_id: dict[str, ModelNode] = {}
        children_by_id: dict[str, list[str]] = {}
        parent_by_id: dict[str, str] = {}
        node_id_by_name: dict[str, str] = {}
        node_id_by_type: dict[str, str] = {}
        instances_by_type_id: dict[str, list[str]] = {}
        root_ids: list[str] = []
        property_to_node: dict[str, str] = {}
        action_to_method: dict[str, tuple[str, str]] = {}

        for source_id, opc_node in by_source_node.items():
            child_sources = child_sources_by_parent.get(source_id, [])
            child_ids = [
                stable_i3x_id(by_source_node[c].node_id, _kind_for_node(by_source_node[c])) for c in child_sources
            ]
            mapped = map_node(opc_node, child_ids)
            nodes_by_id[mapped.id] = mapped
            children_by_id[mapped.id] = child_ids
            for child_id in child_ids:
                parent_by_id[child_id] = mapped.id
            node_id_by_name.setdefault(mapped.name, mapped.id)
            if isinstance(mapped.type, str) and mapped.type:
                node_id_by_type.setdefault(mapped.type, mapped.id)

            if opc_node.parent_node_id is None:
                root_ids.append(mapped.id)

            if mapped.kind == "property":
                property_to_node[mapped.id] = opc_node.node_id
            elif mapped.kind in {"asset", "eventSource"} and mapped.source_type_id is not None:
                instances_by_type_id.setdefault(mapped.source_type_id, []).append(mapped.id)

            if mapped.kind == "action":
                parent = opc_node.parent_node_id
                if parent is not None:
                    action_to_method[mapped.id] = (parent, opc_node.node_id)

        result = BuildResult(
            nodes_by_id={key: value for key, value in nodes_by_id.items()},
            root_ids=root_ids,
            children_by_id=children_by_id,
            instances_by_type_id=instances_by_type_id,
            property_to_node=property_to_node,
            action_to_method=action_to_method,
            parent_by_id=parent_by_id,
            node_id_by_name=node_id_by_name,
            node_id_by_type=node_id_by_type,
        )
        logger.info(
            "Model build phases browse_s=%.3f map_s=%.3f total_s=%.3f source_nodes=%d model_nodes=%d",
            browse_duration_s,
            perf_counter() - map_started,
            perf_counter() - started,
            len(opc_nodes),
            len(result.nodes_by_id),
        )
        return result


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
