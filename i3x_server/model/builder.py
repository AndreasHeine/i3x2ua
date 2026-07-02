from __future__ import annotations

import logging
from datetime import datetime, timezone
from time import perf_counter

from i3x_server.domain.ports.opcua import OpcUaClientProtocol, OpcUaNodeInfo
from i3x_server.model.mapper import classify_opcua_reference_with_confidence, map_node, stable_i3x_id
from i3x_server.model.semantic_profiles import (
    ResolvedProfileSet,
    active_profiles_for_node,
    has_profile_override_for_node,
    resolve_mapping_confidence,
    resolve_namespace_uri,
    resolve_semantic_role,
)
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

        namespace_uri_by_index: dict[int, str] = {}
        get_namespace_infos = getattr(self._opcua_client, "get_namespace_infos", None)
        if callable(get_namespace_infos):
            try:
                namespace_infos = await get_namespace_infos()
                namespace_uri_by_index = {
                    index: namespace_info.uri
                    for index, namespace_info in enumerate(namespace_infos)
                    if isinstance(namespace_info.uri, str)
                }
            except Exception:
                logger.debug("Namespace info resolution failed", exc_info=True)

        child_sources_by_parent: dict[str, list[str]] = {}
        for node in opc_nodes:
            if node.parent_node_id is None:
                continue
            child_sources_by_parent.setdefault(node.parent_node_id, []).append(node.node_id)

        reference_supertypes_by_id: dict[str, list[str]] = {}
        resolve_reference_type_supertypes = getattr(
            self._opcua_client,
            "resolve_reference_type_supertype_browse_names",
            None,
        )
        if callable(resolve_reference_type_supertypes):
            unique_reference_type_ids: set[str] = set()
            for opc_node in opc_nodes:
                for ref in getattr(opc_node, "outgoing_references", []):
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

        child_ids_by_source: dict[str, list[str]] = {}
        mapped_id_by_source: dict[str, str] = {}
        hierarchy_candidate_parents_by_child: dict[str, list[tuple[int, str, str]]] = {}
        composition_candidate_parents_by_child: dict[str, list[tuple[int, str, str]]] = {}
        graph_related_by_source: dict[str, list[tuple[str, str]]] = {}
        graph_relationship_names: set[str] = set()
        parent_id_by_source: dict[str, str | None] = {}
        active_profiles_by_source: dict[str, ResolvedProfileSet] = {}
        namespace_uri_by_source: dict[str, str | None] = {}

        for source_id, opc_node in by_source_node.items():
            mapped_id = stable_i3x_id(opc_node.node_id, _kind_for_node(opc_node))
            mapped_id_by_source[source_id] = mapped_id
            namespace_uri = resolve_namespace_uri(opc_node.node_id, namespace_uri_by_index)
            namespace_uri_by_source[source_id] = namespace_uri
            active_profiles = active_profiles_for_node(opc_node, namespace_uri)
            active_profiles_by_source[source_id] = active_profiles

            child_sources = child_sources_by_parent.get(source_id, [])
            child_ids = [
                stable_i3x_id(
                    by_source_node[child_source].node_id,
                    _kind_for_node(by_source_node[child_source]),
                )
                for child_source in child_sources
            ]
            child_ids_by_source[source_id] = child_ids

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

                relationship_class, confidence = classify_opcua_reference_with_confidence(
                    reference_type_node_id=reference_type_id,
                    reference_browse_name=reference_browse_name,
                    supertype_browse_names=reference_supertypes_by_id.get(reference_type_id)
                    if isinstance(reference_type_id, str)
                    else None,
                    target_node_class=child_node.node_class,
                    profiles=active_profiles.profiles,
                )

                relationship_name = reference_browse_name or (
                    "HasProperty" if child_node.node_class == "Variable" else "Organizes"
                )
                if relationship_class == "hierarchy":
                    hierarchy_candidate_parents_by_child.setdefault(child_id, []).append(
                        (_hierarchy_relationship_priority(relationship_name), source_id, relationship_name)
                    )
                elif relationship_class == "composition":
                    composition_candidate_parents_by_child.setdefault(child_id, []).append(
                        (_hierarchy_relationship_priority(relationship_name), source_id, relationship_name)
                    )
                elif relationship_class == "graph":
                    graph_related_by_source.setdefault(source_id, []).append((relationship_name, child_id))
                    graph_relationship_names.add(relationship_name)
                else:
                    graph_related_by_source.setdefault(source_id, []).append((relationship_name, child_id))
                    graph_relationship_names.add(relationship_name)

        hierarchy_parent_by_id: dict[str, str] = {}
        composition_parent_by_id: dict[str, str] = {}
        hierarchy_children_by_id: dict[str, list[str]] = {
            mapped_id_by_source[source_id]: [] for source_id in by_source_node
        }
        composition_children_by_id: dict[str, list[str]] = {
            mapped_id_by_source[source_id]: [] for source_id in by_source_node
        }
        graph_related_by_id: dict[str, list[tuple[str, str]]] = {
            mapped_id_by_source[source_id]: list(graph_related_by_source.get(source_id, []))
            for source_id in by_source_node
        }
        relationships_by_id: dict[str, dict[str, list[str]]] = {
            mapped_id_by_source[source_id]: {} for source_id in by_source_node
        }

        def append_relationship(source_id: str, relationship_type: str, target_id: str) -> None:
            by_relationship = relationships_by_id.setdefault(source_id, {})
            targets = by_relationship.setdefault(relationship_type, [])
            if target_id not in targets:
                targets.append(target_id)

        for child_id, candidates in hierarchy_candidate_parents_by_child.items():
            selected_priority, selected_parent_id, _selected_relationship_name = sorted(
                candidates,
                key=lambda item: (item[0], item[1], item[2]),
            )[0]
            hierarchy_parent_by_id[child_id] = mapped_id_by_source[selected_parent_id]
            parent_id_by_source[child_id] = selected_parent_id
            hierarchy_children_by_id.setdefault(mapped_id_by_source[selected_parent_id], []).append(child_id)
            if len(candidates) > 1:
                for candidate_priority, candidate_parent_id, _candidate_relationship_name in candidates:
                    if candidate_parent_id == selected_parent_id and candidate_priority == selected_priority:
                        continue
                    graph_related_by_id.setdefault(child_id, []).append(
                        ("OrganizedBy", mapped_id_by_source[candidate_parent_id])
                    )
                    graph_relationship_names.add("OrganizedBy")

        for child_id, candidates in composition_candidate_parents_by_child.items():
            selected_parent_id = sorted(
                candidates,
                key=lambda item: (item[0], item[1], item[2]),
            )[0][1]
            composition_parent_by_id[child_id] = mapped_id_by_source[selected_parent_id]
            composition_children_by_id.setdefault(mapped_id_by_source[selected_parent_id], []).append(child_id)
            parent_id_by_source[child_id] = parent_id_by_source.get(child_id, selected_parent_id)

        for parent_id, child_ids in hierarchy_children_by_id.items():
            for child_id in child_ids:
                append_relationship(parent_id, "HasChildren", child_id)
                append_relationship(child_id, "HasParent", parent_id)

        for parent_id, child_ids in composition_children_by_id.items():
            for child_id in child_ids:
                append_relationship(parent_id, "HasComponent", child_id)
                append_relationship(child_id, "ComponentOf", parent_id)

        for source_id, graph_relations in graph_related_by_id.items():
            for relationship_name, target_id in graph_relations:
                append_relationship(source_id, relationship_name, target_id)
                append_relationship(target_id, f"inverseOf_{relationship_name}", source_id)

        nodes_by_id: dict[str, ModelNode] = {}
        node_id_by_name: dict[str, str] = {}
        node_id_by_type: dict[str, str] = {}
        instances_by_type_id: dict[str, list[str]] = {}
        property_to_node: dict[str, str] = {}
        action_to_method: dict[str, tuple[str, str]] = {}
        root_ids: list[str] = []
        semantic_role_by_id: dict[str, str] = {}
        mapping_confidence_by_id: dict[str, str] = {}
        applied_profile_ids_by_id: dict[str, list[str]] = {}
        namespace_uri_by_id: dict[str, str | None] = {}

        for source_id, opc_node in by_source_node.items():
            mapped_id = mapped_id_by_source[source_id]
            child_ids = child_ids_by_source.get(source_id, [])
            selected_parent_id_resolved = hierarchy_parent_by_id.get(mapped_id)
            if selected_parent_id_resolved is None:
                fallback_parent_source_id = parent_id_by_source.get(mapped_id)
                selected_parent_id_resolved = (
                    mapped_id_by_source.get(fallback_parent_source_id)
                    if fallback_parent_source_id is not None
                    else None
                )
            active_profiles_resolved = active_profiles_by_source.get(source_id)
            namespace_uri = namespace_uri_by_source.get(source_id)
            hierarchy_children = hierarchy_children_by_id.get(mapped_id, [])
            composition_children = composition_children_by_id.get(mapped_id, [])
            graph_relations = graph_related_by_id.get(mapped_id, [])

            semantic_role = resolve_semantic_role(
                opc_node,
                active_profiles_resolved,
                relationship_class="composition" if composition_children else None,
            )
            mapping_confidence = resolve_mapping_confidence(
                opc_node,
                active_profiles_resolved,
                relationship_class="composition"
                if composition_children
                else ("hierarchy" if hierarchy_children else ("graph" if graph_relations else None)),
                has_profile_override=has_profile_override_for_node(opc_node, active_profiles_resolved),
            )

            mapped = map_node(
                opc_node,
                child_ids,
                parent_id=selected_parent_id_resolved,
                is_composition=bool(composition_children),
                semantic_role=semantic_role,
                mapping_confidence=mapping_confidence,
                relationships=relationships_by_id.get(mapped_id, {}),
                metadata={
                    "uaNodeId": opc_node.node_id,
                    "uaTypeNodeId": opc_node.type_definition_id,
                    "namespaceUri": namespace_uri,
                    "appliedProfileIds": list(active_profiles_resolved.profile_ids) if active_profiles_resolved else [],
                },
            )
            nodes_by_id[mapped.id] = mapped
            semantic_role_by_id[mapped.id] = semantic_role
            mapping_confidence_by_id[mapped.id] = mapping_confidence
            applied_profile_ids_by_id[mapped.id] = (
                list(active_profiles_resolved.profile_ids) if active_profiles_resolved else []
            )
            namespace_uri_by_id[mapped.id] = namespace_uri

            node_id_by_name.setdefault(mapped.name, mapped.id)
            if isinstance(mapped.type, str) and mapped.type:
                node_id_by_type.setdefault(mapped.type, mapped.id)

            if selected_parent_id_resolved is None:
                root_ids.append(mapped.id)

            if mapped.kind == "property":
                property_to_node[mapped.id] = opc_node.node_id
            elif mapped.kind in {"asset", "eventSource"} and mapped.source_type_id is not None:
                instances_by_type_id.setdefault(mapped.source_type_id, []).append(mapped.id)

            if mapped.kind == "action" and opc_node.parent_node_id is not None:
                action_to_method[mapped.id] = (opc_node.parent_node_id, opc_node.node_id)

        map_duration_s = perf_counter() - map_started
        total_duration_s = perf_counter() - started
        build_completed_at_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        result = BuildResult(
            nodes_by_id=nodes_by_id,
            root_ids=root_ids,
            children_by_id={
                mapped_id_by_source[source_id]: list(child_ids) for source_id, child_ids in child_ids_by_source.items()
            },
            instances_by_type_id=instances_by_type_id,
            property_to_node=property_to_node,
            action_to_method=action_to_method,
            parent_by_id={node_id: parent_id for node_id, parent_id in hierarchy_parent_by_id.items()},
            node_id_by_name=node_id_by_name,
            node_id_by_type=node_id_by_type,
            hierarchy_children_by_id=hierarchy_children_by_id,
            composition_children_by_id=composition_children_by_id,
            graph_related_by_id=graph_related_by_id,
            relationships_by_id=relationships_by_id,
            graph_relationship_names=graph_relationship_names,
            hierarchy_parent_by_id=hierarchy_parent_by_id,
            composition_parent_by_id=composition_parent_by_id,
            semantic_role_by_id=semantic_role_by_id,
            mapping_confidence_by_id=mapping_confidence_by_id,
            parent_id_by_id={
                node_id: (
                    hierarchy_parent_by_id.get(node_id)
                    if hierarchy_parent_by_id.get(node_id) is not None
                    else (mapped_id_by_source.get(parent_source_id) if parent_source_id is not None else None)
                )
                for node_id in nodes_by_id
                for parent_source_id in [parent_id_by_source.get(node_id)]
            },
            applied_profile_ids_by_id=applied_profile_ids_by_id,
            namespace_uri_by_id=namespace_uri_by_id,
            browse_duration_s=browse_duration_s,
            map_duration_s=map_duration_s,
            total_duration_s=total_duration_s,
            build_completed_at_utc=build_completed_at_utc,
        )
        logger.info(
            "Model build phases browse_s=%.3f map_s=%.3f total_s=%.3f source_nodes=%d model_nodes=%d",
            browse_duration_s,
            map_duration_s,
            total_duration_s,
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


def _hierarchy_relationship_priority(reference_name: str | None) -> int:
    normalized = reference_name.lower().strip() if isinstance(reference_name, str) else ""
    if normalized == "organizes":
        return 0
    if normalized == "hascomponent":
        return 1
    if normalized == "hasorderedcomponent":
        return 2
    return 99
