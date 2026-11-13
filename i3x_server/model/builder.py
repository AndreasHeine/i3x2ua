"""Model builder: browses the address space and assembles the i3X model.

``build`` is the orchestrator; the work is split into sibling phase modules, each owning one
concern:

- :mod:`i3x_server.model.build_phases` - namespace infos, reference-type supertypes, type
  definition hierarchies and the semantic profile sets,
- :mod:`i3x_server.model.reference_graph` - identity mapping, hierarchy/composition parent
  selection, qualified graph relationship ids and inverse pairing,
- :mod:`i3x_server.model.node_mapping` - ``ModelNode`` mapping and the element-id indexes.

Keeping the phases separate keeps the traversal order intact (browse order feeds the mapping,
and the revision hash depends on list order) while each phase can be tested on its own.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from time import perf_counter

from i3x_server.domain.ports.opcua import OpcUaClientProtocol, OpcUaNodeInfo
from i3x_server.model import build_phases, node_mapping, reference_graph
from i3x_server.model.revision import compute_model_revision
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
        by_source_node: dict[str, OpcUaNodeInfo] = {node.node_id: node for node in opc_nodes}

        namespace_infos, namespace_uri_by_index = await build_phases.collect_namespace_infos(self._opcua_client)
        reference_supertypes_by_id = await build_phases.resolve_reference_supertypes(self._opcua_client, opc_nodes)
        type_definition_hierarchy_by_id = await build_phases.resolve_type_hierarchies(
            self._opcua_client,
            opc_nodes,
            namespace_uri_by_index,
        )
        namespace_uri_by_source, active_profiles_by_source = build_phases.build_active_profiles(
            by_source_node,
            type_definition_hierarchy_by_id,
            namespace_uri_by_index,
        )

        graph = reference_graph.build_reference_graph(
            opc_nodes=opc_nodes,
            by_source_node=by_source_node,
            namespace_infos=namespace_infos,
            reference_supertypes_by_id=reference_supertypes_by_id,
            type_definition_hierarchy_by_id=type_definition_hierarchy_by_id,
            active_profiles_by_source=active_profiles_by_source,
        )
        mapping = node_mapping.map_nodes(
            by_source_node=by_source_node,
            graph=graph,
            type_definition_hierarchy_by_id=type_definition_hierarchy_by_id,
            namespace_uri_by_source=namespace_uri_by_source,
            active_profiles_by_source=active_profiles_by_source,
        )
        map_duration_s = perf_counter() - map_started
        element_id_by_node_id, element_id_by_expanded_node_id = node_mapping.build_element_id_index(
            mapping.nodes_by_id,
            namespace_uri_by_index,
        )

        total_duration_s = perf_counter() - started
        result = BuildResult(
            nodes_by_id=mapping.nodes_by_id,
            root_ids=mapping.root_ids,
            children_by_id={node_id: node.children for node_id, node in mapping.nodes_by_id.items()},
            instances_by_type_id=mapping.instances_by_type_id,
            property_to_node=mapping.property_to_node,
            action_to_method=mapping.action_to_method,
            parent_by_id=graph.hierarchy_parent_by_id,
            node_id_by_name=mapping.node_id_by_name,
            node_id_by_type=mapping.node_id_by_type,
            hierarchy_children_by_id=graph.hierarchy_children_by_id,
            composition_children_by_id=graph.composition_children_by_id,
            graph_related_by_id=graph.graph_related_by_id,
            relationships_by_id=graph.relationships_by_id,
            graph_relationship_names=graph.graph_relationship_names,
            graph_relationship_namespace_by_id=graph.graph_relationship_namespace_by_id,
            graph_relationship_display_name_by_id=graph.graph_relationship_display_name_by_id,
            graph_relationship_inverse_by_id=graph.graph_relationship_inverse_by_id,
            hierarchy_parent_by_id=graph.hierarchy_parent_by_id,
            composition_parent_by_id=graph.composition_parent_by_id,
            semantic_role_by_id=mapping.semantic_role_by_id,
            mapping_confidence_by_id=mapping.mapping_confidence_by_id,
            parent_id_by_id=mapping.parent_id_by_id,
            applied_profile_ids_by_id=mapping.applied_profile_ids_by_id,
            namespace_uri_by_id=mapping.namespace_uri_by_id,
            element_id_by_node_id=element_id_by_node_id,
            element_id_by_expanded_node_id=element_id_by_expanded_node_id,
            namespace_uri_by_index=namespace_uri_by_index,
            browse_duration_s=browse_duration_s,
            map_duration_s=map_duration_s,
            total_duration_s=total_duration_s,
            build_completed_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        # Hash off the event loop; large models take noticeable CPU time to serialize.
        result.revision = await asyncio.to_thread(compute_model_revision, result)
        logger.info(
            "Model build phases browse_s=%.3f map_s=%.3f total_s=%.3f source_nodes=%d model_nodes=%d",
            browse_duration_s,
            map_duration_s,
            total_duration_s,
            len(opc_nodes),
            len(result.nodes_by_id),
        )
        return result
