"""Node-mapping phase for ``ModelBuilder.build``.

Maps each browsed node onto an i3X ``ModelNode`` using the relationship graph resolved by
:mod:`i3x_server.model.reference_graph`, and builds the element-id lookup indexes the API
resolves node ids against. ``builder.py`` assembles the ``BuildResult`` from the returned
:class:`NodeMappingResult`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from i3x_server.domain.ports.opcua import OpcUaNodeInfo, OpcUaTypeDefinitionInfo
from i3x_server.domain.utils import canonical_node_id
from i3x_server.model.mapper import map_node
from i3x_server.model.semantic_profiles import (
    has_profile_override_for_node,
    resolve_mapping_confidence,
    resolve_semantic_role,
)
from i3x_server.schemas.i3x import ModelNode

if TYPE_CHECKING:
    from i3x_server.model.reference_graph import GraphResult
    from i3x_server.model.semantic_profiles import ResolvedProfileSet


@dataclass(slots=True)
class NodeMappingResult:
    """Mapped model nodes plus the indexes ``BuildResult`` exposes for them."""

    nodes_by_id: dict[str, ModelNode]
    root_ids: list[str]
    node_id_by_name: dict[str, str]
    node_id_by_type: dict[str, str]
    instances_by_type_id: dict[str, list[str]]
    property_to_node: dict[str, str]
    action_to_method: dict[str, tuple[str, str]]
    semantic_role_by_id: dict[str, str]
    mapping_confidence_by_id: dict[str, str]
    applied_profile_ids_by_id: dict[str, list[str]]
    namespace_uri_by_id: dict[str, str | None]
    parent_id_by_id: dict[str, str | None]


def map_nodes(
    *,
    by_source_node: dict[str, OpcUaNodeInfo],
    graph: GraphResult,
    type_definition_hierarchy_by_id: dict[str, tuple[OpcUaTypeDefinitionInfo, ...]],
    namespace_uri_by_source: dict[str, str | None],
    active_profiles_by_source: dict[str, ResolvedProfileSet],
) -> NodeMappingResult:
    """Map every source node in browse order so the model revision hash stays stable."""
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
        mapped_id = graph.mapped_id_by_source[source_id]
        child_ids = graph.child_ids_by_source.get(source_id, [])
        selected_parent_id_resolved = graph.hierarchy_parent_by_id.get(mapped_id)
        if selected_parent_id_resolved is None:
            fallback_parent_source_id = graph.parent_id_by_source.get(mapped_id)
            selected_parent_id_resolved = (
                graph.mapped_id_by_source.get(fallback_parent_source_id)
                if fallback_parent_source_id is not None
                else None
            )
        active_profiles_resolved = active_profiles_by_source.get(source_id)
        namespace_uri = namespace_uri_by_source.get(source_id)
        hierarchy_children = graph.hierarchy_children_by_id.get(mapped_id, [])
        composition_children = graph.composition_children_by_id.get(mapped_id, [])
        graph_relations = graph.graph_related_by_id.get(mapped_id, [])

        semantic_role = resolve_semantic_role(
            opc_node,
            active_profiles_resolved,
            type_definition_hierarchy=type_definition_hierarchy_by_id.get(
                opc_node.type_definition_id or "",
                (),
            ),
        )
        mapping_confidence = resolve_mapping_confidence(
            opc_node,
            active_profiles_resolved,
            relationship_class="composition"
            if composition_children
            else ("hierarchy" if hierarchy_children else ("graph" if graph_relations else None)),
            has_profile_override=has_profile_override_for_node(
                opc_node,
                active_profiles_resolved,
                type_definition_hierarchy_by_id.get(
                    opc_node.type_definition_id or "",
                    (),
                ),
            ),
        )

        applied_profile_ids = list(active_profiles_resolved.profile_ids) if active_profiles_resolved else []
        mapped = map_node(
            opc_node,
            child_ids,
            element_id=mapped_id,
            parent_id=selected_parent_id_resolved,
            is_composition=bool(composition_children),
            semantic_role=semantic_role,
            mapping_confidence=mapping_confidence,
            relationships=graph.relationships_by_id.get(mapped_id, {}),
            metadata={
                "uaNodeId": opc_node.node_id,
                "expandedNodeId": graph.identity_node_id_by_source[source_id],
                "uaTypeNodeId": opc_node.type_definition_id,
                "namespaceUri": namespace_uri,
                "appliedProfileIds": applied_profile_ids,
                **({"description": opc_node.description} if opc_node.description is not None else {}),
                **({"accessLevel": opc_node.access_level} if opc_node.access_level is not None else {}),
                **({"userAccessLevel": opc_node.user_access_level} if opc_node.user_access_level is not None else {}),
                **(
                    {"writable": bool(opc_node.user_access_level & 0x02)}
                    if opc_node.user_access_level is not None
                    else {}
                ),
                **({"executable": opc_node.executable} if opc_node.executable is not None else {}),
                **({"userExecutable": opc_node.user_executable} if opc_node.user_executable is not None else {}),
            },
        )
        nodes_by_id[mapped.id] = mapped
        graph.relationships_by_id[mapped.id] = mapped.relationships
        semantic_role_by_id[mapped.id] = semantic_role
        mapping_confidence_by_id[mapped.id] = mapping_confidence
        applied_profile_ids_by_id[mapped.id] = applied_profile_ids
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

    # The hierarchy parent wins; nodes without one fall back to the source parent the graph
    # recorded (composition-only children keep their OPC UA parent for API navigation).
    parent_id_by_id: dict[str, str | None] = {}
    for node_id in nodes_by_id:
        hierarchy_parent = graph.hierarchy_parent_by_id.get(node_id)
        if hierarchy_parent is not None:
            parent_id_by_id[node_id] = hierarchy_parent
            continue
        parent_source_id = graph.parent_id_by_source.get(node_id)
        parent_id_by_id[node_id] = (
            graph.mapped_id_by_source.get(parent_source_id) if parent_source_id is not None else None
        )

    return NodeMappingResult(
        nodes_by_id=nodes_by_id,
        root_ids=root_ids,
        node_id_by_name=node_id_by_name,
        node_id_by_type=node_id_by_type,
        instances_by_type_id=instances_by_type_id,
        property_to_node=property_to_node,
        action_to_method=action_to_method,
        semantic_role_by_id=semantic_role_by_id,
        mapping_confidence_by_id=mapping_confidence_by_id,
        applied_profile_ids_by_id=applied_profile_ids_by_id,
        namespace_uri_by_id=namespace_uri_by_id,
        parent_id_by_id=parent_id_by_id,
    )


def build_element_id_index(
    nodes_by_id: dict[str, ModelNode],
    namespace_uri_by_index: dict[int, str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Element-id lookup by raw UA node id and by expanded node id.

    Canonical ids are indexed alongside the ids stored in the node metadata, so API lookups
    accept every spelling the address space produces. First occurrence wins, which keeps
    duplicates of the same node id pointing at the same element.
    """
    element_id_by_node_id: dict[str, str] = {}
    element_id_by_expanded_node_id: dict[str, str] = {}
    for model_node in nodes_by_id.values():
        ua_node_id = model_node.metadata.get("uaNodeId")
        expanded_id = model_node.metadata.get("expandedNodeId")
        if isinstance(ua_node_id, str) and ua_node_id:
            element_id_by_node_id.setdefault(ua_node_id, model_node.id)
            canonical_id = canonical_node_id(ua_node_id, namespace_uri_by_index)
            if canonical_id is not None:
                destination = (
                    element_id_by_expanded_node_id if canonical_id.startswith("nsu=") else element_id_by_node_id
                )
                destination.setdefault(canonical_id, model_node.id)
        if isinstance(expanded_id, str) and expanded_id:
            element_id_by_expanded_node_id.setdefault(expanded_id, model_node.id)
            canonical_expanded_id = canonical_node_id(expanded_id, namespace_uri_by_index)
            if canonical_expanded_id is not None:
                element_id_by_expanded_node_id.setdefault(canonical_expanded_id, model_node.id)
    return element_id_by_node_id, element_id_by_expanded_node_id
