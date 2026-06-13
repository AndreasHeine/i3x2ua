from __future__ import annotations

import logging
from time import perf_counter

from i3x_server.model.mapper import classify_opcua_reference, map_node, stable_i3x_id
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
        hierarchy_children_by_id: dict[str, list[str]] = {}
        composition_children_by_id: dict[str, list[str]] = {}
        graph_related_by_id: dict[str, list[tuple[str, str]]] = {}
        relationships_by_id: dict[str, dict[str, list[str]]] = {}
        graph_relationship_names: set[str] = set()
        hierarchy_parent_by_id: dict[str, str] = {}
        composition_parent_by_id: dict[str, str] = {}
        reference_supertypes_by_id: dict[str, list[str]] = {}
        root_ids: list[str] = []
        property_to_node: dict[str, str] = {}
        action_to_method: dict[str, tuple[str, str]] = {}

        resolve_reference_type_supertypes = getattr(
            self._opcua_client,
            "resolve_reference_type_supertype_browse_names",
            None,
        )
        if callable(resolve_reference_type_supertypes):
            unique_reference_type_ids: set[str] = set()
            for opc_node in opc_nodes:
                outgoing_refs = getattr(opc_node, "outgoing_references", [])
                for ref in outgoing_refs:
                    reference_type_id = getattr(ref, "reference_type_id", None)
                    if isinstance(reference_type_id, str) and reference_type_id:
                        unique_reference_type_ids.add(reference_type_id)

            for reference_type_id in unique_reference_type_ids:
                try:
                    resolved_supertypes = await resolve_reference_type_supertypes(reference_type_id)
                    if isinstance(resolved_supertypes, list):
                        reference_supertypes_by_id[reference_type_id] = [
                            name for name in resolved_supertypes if isinstance(name, str)
                        ]
                except Exception:
                    logger.debug(
                        "Reference supertype resolution failed for reference_type_id=%s",
                        reference_type_id,
                        exc_info=True,
                    )

        def append_relationship(source_id: str, relationship_type: str, target_id: str) -> None:
            by_relationship = relationships_by_id.setdefault(source_id, {})
            targets = by_relationship.setdefault(relationship_type, [])
            if target_id not in targets:
                targets.append(target_id)

        for source_id, opc_node in by_source_node.items():
            child_sources = child_sources_by_parent.get(source_id, [])
            child_ids: list[str] = []
            hierarchy_child_ids: list[str] = []
            composition_child_ids: list[str] = []
            graph_related: list[tuple[str, str]] = []

            parent_mapped_id = stable_i3x_id(opc_node.node_id, _kind_for_node(opc_node))

            for child_source in child_sources:
                child_node = by_source_node[child_source]
                child_id = stable_i3x_id(child_node.node_id, _kind_for_node(child_node))
                child_ids.append(child_id)

            reference_entries: list[tuple[str, str | None, str | None]] = []
            outgoing_refs = getattr(opc_node, "outgoing_references", [])
            if outgoing_refs:
                for ref in outgoing_refs:
                    target_node_id = getattr(ref, "target_node_id", None)
                    if not isinstance(target_node_id, str) or target_node_id not in by_source_node:
                        continue
                    reference_entries.append(
                        (
                            target_node_id,
                            getattr(ref, "reference_type_id", None),
                            getattr(ref, "reference_browse_name", None),
                        )
                    )
            else:
                for child_source in child_sources:
                    child_node = by_source_node[child_source]
                    fallback_reference_browse_name = (
                        "HasProperty" if child_node.node_class == "Variable" else "Organizes"
                    )
                    reference_entries.append((child_source, None, fallback_reference_browse_name))

            seen_relationship_edges: set[tuple[str, str]] = set()
            for target_source_id, reference_type_id, reference_browse_name in reference_entries:
                child_node = by_source_node[target_source_id]
                child_id = stable_i3x_id(child_node.node_id, _kind_for_node(child_node))
                if (target_source_id, child_id) in seen_relationship_edges:
                    continue
                seen_relationship_edges.add((target_source_id, child_id))

                relationship_class = classify_opcua_reference(
                    reference_type_node_id=reference_type_id,
                    reference_browse_name=reference_browse_name,
                    supertype_browse_names=reference_supertypes_by_id.get(reference_type_id)
                    if isinstance(reference_type_id, str)
                    else None,
                    target_node_class=child_node.node_class,
                )

                if relationship_class == "composition":
                    composition_child_ids.append(child_id)
                    composition_parent_by_id[child_id] = parent_mapped_id
                elif relationship_class == "hierarchy":
                    hierarchy_child_ids.append(child_id)
                    hierarchy_parent_by_id[child_id] = parent_mapped_id
                elif relationship_class == "graph":
                    graph_name = reference_browse_name or "RelatedTo"
                    graph_related.append((graph_name, child_id))
                    graph_relationship_names.add(graph_name)

            mapped = map_node(opc_node, child_ids)
            nodes_by_id[mapped.id] = mapped
            children_by_id[mapped.id] = child_ids
            hierarchy_children_by_id[mapped.id] = hierarchy_child_ids
            composition_children_by_id[mapped.id] = composition_child_ids
            graph_related_by_id[mapped.id] = graph_related
            relationships_by_id.setdefault(mapped.id, {})

            for child_id in hierarchy_child_ids:
                append_relationship(mapped.id, "HasChildren", child_id)
                append_relationship(child_id, "HasParent", mapped.id)
            for child_id in composition_child_ids:
                append_relationship(mapped.id, "HasComponent", child_id)
                append_relationship(child_id, "ComponentOf", mapped.id)
            for relationship_name, child_id in graph_related:
                append_relationship(mapped.id, relationship_name, child_id)
                append_relationship(child_id, f"inverseOf_{relationship_name}", mapped.id)

            for child_id in child_ids:
                if child_id in hierarchy_child_ids:
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
            hierarchy_children_by_id=hierarchy_children_by_id,
            composition_children_by_id=composition_children_by_id,
            graph_related_by_id=graph_related_by_id,
            relationships_by_id=relationships_by_id,
            graph_relationship_names=graph_relationship_names,
            hierarchy_parent_by_id=hierarchy_parent_by_id,
            composition_parent_by_id=composition_parent_by_id,
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
