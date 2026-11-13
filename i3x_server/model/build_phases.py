"""Address-space input phases for ``ModelBuilder.build``.

Namespace infos, reference-type supertype chains, type definition hierarchies, and the
semantic profile sets are resolved here so ``builder.py`` stays the orchestrator. The
client seams are optional (test doubles may not implement them), and resolution
failures degrade to warnings: a partially resolved model is still servable, and only
BaseException (cancellation) escapes.
"""

from __future__ import annotations

import asyncio
import logging
from time import perf_counter

from i3x_server.domain.ports.opcua import (
    OpcUaClientProtocol,
    OpcUaNamespaceInfo,
    OpcUaNodeInfo,
    OpcUaTypeDefinitionInfo,
)
from i3x_server.model.semantic_profiles import (
    ResolvedProfileSet,
    active_profiles_for_node,
    resolve_namespace_uri,
)

logger = logging.getLogger(__name__)


async def collect_namespace_infos(
    opcua_client: OpcUaClientProtocol,
) -> tuple[list[OpcUaNamespaceInfo], dict[int, str]]:
    """Namespace infos plus the index -> URI lookup used for canonical node ids."""
    namespace_infos: list[OpcUaNamespaceInfo] = []
    get_namespace_infos = getattr(opcua_client, "get_namespace_infos", None)
    if callable(get_namespace_infos):
        try:
            namespace_infos = await get_namespace_infos()
        except Exception:
            logger.warning("Namespace info resolution failed", exc_info=True)
    namespace_uri_by_index = {
        index: namespace_info.uri
        for index, namespace_info in enumerate(namespace_infos)
        if isinstance(namespace_info.uri, str) and namespace_info.uri
    }
    return namespace_infos, namespace_uri_by_index


async def resolve_reference_supertypes(
    opcua_client: OpcUaClientProtocol,
    opc_nodes: list[OpcUaNodeInfo],
) -> dict[str, list[str]]:
    """Supertype browse-name chains for every distinct reference type in the tree."""
    reference_supertypes_by_id: dict[str, list[str]] = {}
    resolve_reference_type_supertypes = getattr(
        opcua_client,
        "resolve_reference_type_supertype_browse_names",
        None,
    )
    if not callable(resolve_reference_type_supertypes):
        return reference_supertypes_by_id

    unique_reference_type_ids: set[str] = set()
    for opc_node in opc_nodes:
        for ref in getattr(opc_node, "outgoing_references", []):
            reference_type_id = getattr(ref, "reference_type_id", None)
            if isinstance(reference_type_id, str) and reference_type_id:
                unique_reference_type_ids.add(reference_type_id)

    # Resolve distinct reference-type chains concurrently; chains that converge on
    # shared ancestors are cached per node, so overlap costs at most one extra browse.
    reference_type_ids = sorted(unique_reference_type_ids)
    if not reference_type_ids:
        return reference_supertypes_by_id
    resolved_results = await asyncio.gather(
        *(resolve_reference_type_supertypes(reference_type_id) for reference_type_id in reference_type_ids),
        return_exceptions=True,
    )
    for index, reference_type_id in enumerate(reference_type_ids):
        resolved_supertypes = resolved_results[index]
        if isinstance(resolved_supertypes, BaseException):
            if not isinstance(resolved_supertypes, Exception):
                raise resolved_supertypes
            logger.warning(
                "Reference supertype resolution failed for reference_type_id=%s",
                reference_type_id,
                exc_info=resolved_supertypes,
            )
            continue
        if isinstance(resolved_supertypes, list):
            reference_supertypes_by_id[reference_type_id] = [
                name for name in resolved_supertypes if isinstance(name, str)
            ]
    return reference_supertypes_by_id


async def resolve_type_hierarchies(
    opcua_client: OpcUaClientProtocol,
    opc_nodes: list[OpcUaNodeInfo],
    namespace_uri_by_index: dict[int, str],
) -> dict[str, tuple[OpcUaTypeDefinitionInfo, ...]]:
    """Type definition supertype chains for every type the instances reference.

    The batch seam is preferred; on failure (or without the seam) individual
    resolution is used, so one broken chain cannot poison the whole model.
    """
    type_definition_hierarchy_by_id: dict[str, tuple[OpcUaTypeDefinitionInfo, ...]] = {}
    type_resolution_started = perf_counter()
    resolve_type_definition_hierarchies = getattr(opcua_client, "resolve_type_definition_hierarchies", None)
    resolve_type_definition_hierarchy = getattr(opcua_client, "resolve_type_definition_hierarchy", None)
    if callable(resolve_type_definition_hierarchies) or callable(resolve_type_definition_hierarchy):
        unique_type_definition_ids = {node.type_definition_id for node in opc_nodes if node.type_definition_id}
        resolved_hierarchies: dict[str, list[OpcUaTypeDefinitionInfo]] | None = None
        if callable(resolve_type_definition_hierarchies):
            try:
                resolved_hierarchies = await resolve_type_definition_hierarchies(sorted(unique_type_definition_ids))
            except Exception:
                logger.warning(
                    "Batch type definition hierarchy resolution failed; retrying individual types",
                    exc_info=True,
                )
        for type_definition_id in unique_type_definition_ids:
            try:
                if resolved_hierarchies is not None:
                    if type_definition_id not in resolved_hierarchies:
                        continue
                    resolved_hierarchy = resolved_hierarchies[type_definition_id]
                elif callable(resolve_type_definition_hierarchy):
                    resolved_hierarchy = await resolve_type_definition_hierarchy(type_definition_id)
                else:
                    continue
                if isinstance(resolved_hierarchy, list):
                    type_definition_hierarchy_by_id[type_definition_id] = tuple(
                        OpcUaTypeDefinitionInfo(
                            node_id=type_info.node_id,
                            browse_name=type_info.browse_name,
                            namespace_uri=type_info.namespace_uri
                            or resolve_namespace_uri(type_info.node_id, namespace_uri_by_index),
                        )
                        for type_info in resolved_hierarchy
                        if isinstance(type_info, OpcUaTypeDefinitionInfo)
                    )
            except Exception:
                logger.warning(
                    "Type definition hierarchy resolution failed for type_definition_id=%s",
                    type_definition_id,
                    exc_info=True,
                )
    logger.info(
        "Model type hierarchy resolution duration_s=%.3f resolved_instance_types=%d",
        perf_counter() - type_resolution_started,
        len(type_definition_hierarchy_by_id),
    )
    return type_definition_hierarchy_by_id


def build_active_profiles(
    by_source_node: dict[str, OpcUaNodeInfo],
    type_definition_hierarchy_by_id: dict[str, tuple[OpcUaTypeDefinitionInfo, ...]],
    namespace_uri_by_index: dict[int, str],
) -> tuple[dict[str, str | None], dict[str, ResolvedProfileSet]]:
    """Namespace URI and semantic profile set per source node, keyed by source node id."""
    namespace_uri_by_source: dict[str, str | None] = {}
    active_profiles_by_source: dict[str, ResolvedProfileSet] = {}
    for source_id, opc_node in by_source_node.items():
        namespace_uri = resolve_namespace_uri(opc_node.node_id, namespace_uri_by_index)
        namespace_uri_by_source[source_id] = namespace_uri
        type_definition_hierarchy = type_definition_hierarchy_by_id.get(
            opc_node.type_definition_id or "",
            (),
        )
        active_profiles_by_source[source_id] = active_profiles_for_node(
            opc_node,
            type_definition_hierarchy,
        )
    return namespace_uri_by_source, active_profiles_by_source
