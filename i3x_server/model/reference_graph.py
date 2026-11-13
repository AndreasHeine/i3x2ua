"""Reference-graph phase for ``ModelBuilder.build``.

Turns the browsed address space into the i3X relationship graph: identity mapping
(``expandedNodeId`` -> ``stable_i3x_id``), hierarchy/composition parent selection,
qualified graph relationship ids, and the inverse pairing required by i3X.
``builder.py`` supplies the resolved inputs (namespaces, reference supertypes, type
hierarchies, profiles) and assembles the ``BuildResult`` from the returned
:class:`GraphResult`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from i3x_server.domain.ports.opcua import (
    OpcUaNamespaceInfo,
    OpcUaNodeInfo,
    OpcUaTypeDefinitionInfo,
)
from i3x_server.domain.utils import I3X_NAMESPACE_URI, expanded_node_id, namespace_uri_from_expanded_node_id
from i3x_server.model.mapper import classify_opcua_reference_with_confidence, infer_kind, stable_i3x_id
from i3x_server.model.semantic_profiles import forced_relationship_class_for_node

if TYPE_CHECKING:
    from i3x_server.model.semantic_profiles import ResolvedProfileSet

logger = logging.getLogger(__name__)

# Reserved i3X relationship ids: their semantics are bound to ``parentId`` and
# ``isComposition`` (IMPLEMENTATION_GUIDE.md:450/:467), so they never label graph edges.
_RESERVED_GRAPH_RELATIONSHIP_IDS = frozenset({"HasParent", "HasChildren", "HasComponent", "ComponentOf", "OrganizedBy"})


@dataclass(slots=True)
class GraphResult:
    """Everything the graph phase derives for the node mapping and ``BuildResult``.

    Keys are OPC UA source node ids where the phase works before mapping (``*_by_source``)
    and i3X element ids afterwards (``*_by_id``).
    """

    identity_node_id_by_source: dict[str, str]
    mapped_id_by_source: dict[str, str]
    child_ids_by_source: dict[str, list[str]]
    parent_id_by_source: dict[str, str | None]
    hierarchy_parent_by_id: dict[str, str]
    composition_parent_by_id: dict[str, str]
    hierarchy_children_by_id: dict[str, list[str]]
    composition_children_by_id: dict[str, list[str]]
    graph_related_by_id: dict[str, list[tuple[str, str]]]
    relationships_by_id: dict[str, dict[str, list[str]]]
    graph_relationship_names: set[str]
    graph_relationship_namespace_by_id: dict[str, str]
    graph_relationship_display_name_by_id: dict[str, str]
    graph_relationship_inverse_by_id: dict[str, str]


def _hierarchy_relationship_priority(reference_name: str | None) -> int:
    normalized = reference_name.lower().strip() if isinstance(reference_name, str) else ""
    if normalized == "organizes":
        return 0
    if normalized == "hascomponent":
        return 1
    if normalized == "hasorderedcomponent":
        return 2
    return 99


def build_reference_graph(
    *,
    opc_nodes: list[OpcUaNodeInfo],
    by_source_node: dict[str, OpcUaNodeInfo],
    namespace_infos: list[OpcUaNamespaceInfo],
    reference_supertypes_by_id: dict[str, list[str]],
    type_definition_hierarchy_by_id: dict[str, tuple[OpcUaTypeDefinitionInfo, ...]],
    active_profiles_by_source: dict[str, ResolvedProfileSet],
) -> GraphResult:
    """Classify every outgoing reference and resolve parents, edges, and inverses.

    Iteration order follows the browse order (``by_source_node``), so relationship and
    children lists stay deterministic across builds - the model revision hash depends on
    their order.
    """
    child_sources_by_parent: dict[str, list[str]] = {}
    for node in opc_nodes:
        if node.parent_node_id is None:
            continue
        child_sources_by_parent.setdefault(node.parent_node_id, []).append(node.node_id)

    identity_node_id_by_source = {
        source_id: expanded_node_id(opc_node.node_id, namespace_infos) for source_id, opc_node in by_source_node.items()
    }
    mapped_id_by_source: dict[str, str] = {}
    child_ids_by_source: dict[str, list[str]] = {}
    hierarchy_candidate_parents_by_child: dict[str, list[tuple[int, str, str]]] = {}
    composition_candidate_parents_by_child: dict[str, list[tuple[int, str, str]]] = {}
    graph_related_by_source: dict[str, list[tuple[str, str, str]]] = {}
    graph_namespaces_by_name: dict[str, set[str]] = {}
    graph_inverse_names_by_name: dict[tuple[str, str], str] = {}

    def register_graph_relationship(name: str, reference_type_id: str | None) -> str:
        namespace_uri: str | None
        if reference_type_id is None:
            namespace_uri = I3X_NAMESPACE_URI
        else:
            namespace_uri = namespace_uri_from_expanded_node_id(expanded_node_id(reference_type_id, namespace_infos))
            if namespace_uri is None:
                raise ValueError(f"Cannot resolve namespace for reference type {reference_type_id!r}")
        graph_namespaces_by_name.setdefault(name, set()).add(namespace_uri)
        return namespace_uri

    parent_id_by_source: dict[str, str | None] = {}

    for source_id, opc_node in by_source_node.items():
        mapped_id = stable_i3x_id(identity_node_id_by_source[source_id], infer_kind(opc_node))
        mapped_id_by_source[source_id] = mapped_id
        active_profiles = active_profiles_by_source[source_id]

        child_sources = child_sources_by_parent.get(source_id, [])
        child_ids = [
            stable_i3x_id(
                identity_node_id_by_source[child_source],
                infer_kind(by_source_node[child_source]),
            )
            for child_source in child_sources
        ]
        child_ids_by_source[source_id] = child_ids

        reference_entries: list[tuple[str, str | None, str | None, str | None]] = []
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
                        getattr(ref, "reference_inverse_name", None),
                    )
                )
        else:
            for child_source in child_sources:
                child_node = by_source_node[child_source]
                fallback_reference_browse_name = "HasProperty" if child_node.node_class == "Variable" else "Organizes"
                reference_entries.append((child_source, None, fallback_reference_browse_name, None))

        seen_relationship_edges: set[tuple[str, str | None]] = set()
        for (
            target_source_id,
            reference_type_id,
            reference_browse_name,
            reference_inverse_name,
        ) in reference_entries:
            child_node = by_source_node[target_source_id]
            child_id = stable_i3x_id(identity_node_id_by_source[target_source_id], infer_kind(child_node))
            if (target_source_id, reference_type_id) in seen_relationship_edges:
                continue
            seen_relationship_edges.add((target_source_id, reference_type_id))

            relationship_class, confidence = classify_opcua_reference_with_confidence(
                reference_type_node_id=reference_type_id,
                reference_browse_name=reference_browse_name,
                supertype_browse_names=reference_supertypes_by_id.get(reference_type_id)
                if isinstance(reference_type_id, str)
                else None,
                target_node_class=child_node.node_class,
                profiles=active_profiles.profiles,
            )
            forced_relationship_class = forced_relationship_class_for_node(
                child_node,
                active_profiles_by_source.get(target_source_id),
                type_definition_hierarchy_by_id.get(child_node.type_definition_id or "", ()),
            )
            if forced_relationship_class is not None:
                relationship_class = forced_relationship_class

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
            else:
                reference_namespace = register_graph_relationship(relationship_name, reference_type_id)
                if isinstance(reference_inverse_name, str) and reference_inverse_name:
                    graph_inverse_names_by_name.setdefault(
                        (relationship_name, reference_namespace),
                        reference_inverse_name,
                    )
                graph_related_by_source.setdefault(source_id, []).append(
                    (relationship_name, reference_namespace, child_id)
                )

    hierarchy_parent_by_id: dict[str, str] = {}
    composition_parent_by_id: dict[str, str] = {}
    hierarchy_children_by_id: dict[str, list[str]] = {
        mapped_id_by_source[source_id]: [] for source_id in by_source_node
    }
    composition_children_by_id: dict[str, list[str]] = {
        mapped_id_by_source[source_id]: [] for source_id in by_source_node
    }
    graph_entries_by_id: dict[str, list[tuple[str, str, str]]] = {
        mapped_id_by_source[source_id]: list(graph_related_by_source.get(source_id, [])) for source_id in by_source_node
    }
    relationships_by_id: dict[str, dict[str, list[str]]] = {
        mapped_id_by_source[source_id]: {} for source_id in by_source_node
    }

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
                reference_namespace = register_graph_relationship("OrganizedBy", None)
                graph_entries_by_id.setdefault(child_id, []).append(
                    ("OrganizedBy", reference_namespace, mapped_id_by_source[candidate_parent_id])
                )

    for child_id, candidates in composition_candidate_parents_by_child.items():
        selected_parent_id = sorted(
            candidates,
            key=lambda item: (item[0], item[1], item[2]),
        )[0][1]
        composition_parent_by_id[child_id] = mapped_id_by_source[selected_parent_id]
        composition_children_by_id.setdefault(mapped_id_by_source[selected_parent_id], []).append(child_id)
        parent_id_by_source[child_id] = parent_id_by_source.get(child_id, selected_parent_id)

    graph_relationship_ids: dict[tuple[str, str], str] = {}
    graph_relationship_namespace_by_id: dict[str, str] = {}
    graph_relationship_display_name_by_id: dict[str, str] = {}
    for name, namespaces in sorted(graph_namespaces_by_name.items()):
        qualify = (
            len(namespaces) > 1
            or name in {"HasParent", "HasChildren", "HasComponent", "ComponentOf"}
            or name.startswith("inverseOf_")
            or ":" in name
        )
        for namespace_uri in sorted(namespaces):
            relationship_id = f"{namespace_uri}:{name}" if qualify else name
            if relationship_id in graph_relationship_namespace_by_id:
                raise ValueError(f"Expanded qualified relationship ID {relationship_id!r} is ambiguous")
            graph_relationship_ids[(name, namespace_uri)] = relationship_id
            graph_relationship_namespace_by_id[relationship_id] = namespace_uri
            graph_relationship_display_name_by_id[relationship_id] = name
    graph_relationship_names = set(graph_relationship_namespace_by_id)
    graph_related_by_id = {
        source_id: [
            (graph_relationship_ids[(name, namespace_uri)], target_id) for name, namespace_uri, target_id in entries
        ]
        for source_id, entries in graph_entries_by_id.items()
    }

    for parent_id, child_ids in hierarchy_children_by_id.items():
        for child_id in child_ids:
            _append_relationship(relationships_by_id, parent_id, "HasChildren", child_id)
            _append_relationship(relationships_by_id, child_id, "HasParent", parent_id)

    for parent_id, child_ids in composition_children_by_id.items():
        for child_id in child_ids:
            _append_relationship(relationships_by_id, parent_id, "HasComponent", child_id)
            _append_relationship(relationships_by_id, child_id, "ComponentOf", parent_id)

    # Inverse pairing follows the i3X rules (IMPLEMENTATION_GUIDE.md:397/:434): reserved
    # hierarchy/composition names never label graph edges, natural inverse names are reused
    # only for mutual pairs, and reverse-only ids stay unique per forward relationship.
    graph_ids_by_direction: dict[tuple[str, str], set[str]] = {}
    for source_id, entries in graph_entries_by_id.items():
        for name, namespace_uri, target_id in entries:
            graph_ids_by_direction.setdefault((source_id, target_id), set()).add(
                graph_relationship_ids[(name, namespace_uri)]
            )

    graph_relationship_inverse_by_id = _derive_graph_inverses(
        graph_related_by_id=graph_related_by_id,
        graph_ids_by_direction=graph_ids_by_direction,
        graph_relationship_ids=graph_relationship_ids,
        graph_relationship_namespace_by_id=graph_relationship_namespace_by_id,
        graph_relationship_display_name_by_id=graph_relationship_display_name_by_id,
        graph_inverse_names_by_name=graph_inverse_names_by_name,
    )

    for source_id, graph_relations in graph_related_by_id.items():
        for relationship_name, target_id in graph_relations:
            _append_relationship(relationships_by_id, source_id, relationship_name, target_id)
            _append_relationship(
                relationships_by_id,
                target_id,
                graph_relationship_inverse_by_id[relationship_name],
                source_id,
            )

    return GraphResult(
        identity_node_id_by_source=identity_node_id_by_source,
        mapped_id_by_source=mapped_id_by_source,
        child_ids_by_source=child_ids_by_source,
        parent_id_by_source=parent_id_by_source,
        hierarchy_parent_by_id=hierarchy_parent_by_id,
        composition_parent_by_id=composition_parent_by_id,
        hierarchy_children_by_id=hierarchy_children_by_id,
        composition_children_by_id=composition_children_by_id,
        graph_related_by_id=graph_related_by_id,
        relationships_by_id=relationships_by_id,
        graph_relationship_names=graph_relationship_names,
        graph_relationship_namespace_by_id=graph_relationship_namespace_by_id,
        graph_relationship_display_name_by_id=graph_relationship_display_name_by_id,
        graph_relationship_inverse_by_id=graph_relationship_inverse_by_id,
    )


def _append_relationship(
    relationships_by_id: dict[str, dict[str, list[str]]],
    source_id: str,
    relationship_type: str,
    target_id: str,
) -> None:
    by_relationship = relationships_by_id.setdefault(source_id, {})
    targets = by_relationship.setdefault(relationship_type, [])
    if target_id not in targets:
        targets.append(target_id)


def _derive_graph_inverses(
    *,
    graph_related_by_id: dict[str, list[tuple[str, str]]],
    graph_ids_by_direction: dict[tuple[str, str], set[str]],
    graph_relationship_ids: dict[tuple[str, str], str],
    graph_relationship_namespace_by_id: dict[str, str],
    graph_relationship_display_name_by_id: dict[str, str],
    graph_inverse_names_by_name: dict[tuple[str, str], str],
) -> dict[str, str]:
    """Pair every graph relationship with the relationship id of its inverse.

    i3X requires a registered, symmetric ``reverseOf`` for every relationship type
    (IMPLEMENTATION_GUIDE.md:397) and bidirectional edge storage (:434). Only a few i3X names
    overlap OPC UA reference names, so inverse ids are allocated with three rules:

    - reserved hierarchy/composition names never become graph inverses (their semantics are bound
      to ``parentId`` and ``isComposition``, :450/:467);
    - a name published by the address space - a modelled opposite edge or the ``InverseName``
      attribute (OPC UA Part 3, e.g. ``HasNotifier`` -> ``NotifierOf``) - is reused only when the
      pair is mutual, otherwise a shared or foreign inverse would break the registry chain;
    - the reverse otherwise stays unique per forward relationship: bare browse name when it is
      unambiguous, ``<namespace>:<name>`` when the bare name is already taken (the forward
      ReferenceType defines its inverse, so its namespace is the correct owner), and
      ``inverseOf_<forward>`` as the last fallback.
    """
    namespace_by_relationship = {
        relationship_id: namespace_uri for (name, namespace_uri), relationship_id in graph_relationship_ids.items()
    }
    candidates: dict[str, str | None] = {}
    raw_inverse_names: dict[str, str] = {}

    for source_id in sorted(graph_related_by_id):
        for relationship_id, target_id in sorted(graph_related_by_id[source_id]):
            if relationship_id in _RESERVED_GRAPH_RELATIONSHIP_IDS:
                candidates[relationship_id] = None
                continue
            opposite = graph_ids_by_direction.get((target_id, source_id), set()) - {relationship_id}
            if len(opposite) == 1:
                opposite_candidate = next(iter(opposite))
                if opposite_candidate in _RESERVED_GRAPH_RELATIONSHIP_IDS:
                    logger.warning(
                        "Graph relationship %r models reserved reverse %r; using a synthetic inverse",
                        relationship_id,
                        opposite_candidate,
                    )
                    candidates[relationship_id] = None
                    continue
                candidates[relationship_id] = opposite_candidate
                continue
            forward_display = graph_relationship_display_name_by_id.get(relationship_id, relationship_id)
            forward_namespace = namespace_by_relationship.get(relationship_id)
            if forward_namespace is None:
                candidates[relationship_id] = None
                continue
            attribute_inverse = graph_inverse_names_by_name.get((forward_display, forward_namespace))
            if not attribute_inverse or attribute_inverse == forward_display:
                candidates[relationship_id] = None
                continue
            if attribute_inverse in _RESERVED_GRAPH_RELATIONSHIP_IDS:
                logger.warning(
                    "Ignoring InverseName %r for graph relationship %r: reserved i3X relationship name",
                    attribute_inverse,
                    relationship_id,
                )
                candidates[relationship_id] = None
                continue
            raw_inverse_names[relationship_id] = attribute_inverse
            candidates[relationship_id] = graph_relationship_ids.get(
                (attribute_inverse, forward_namespace), attribute_inverse
            )

    inverse_by_id: dict[str, str] = {}
    claimed = set(graph_relationship_namespace_by_id)
    for relationship_id in sorted(candidates):
        candidate = candidates[relationship_id]
        if candidate is None or candidate == relationship_id:
            inverse_by_id[relationship_id] = _synthetic_inverse_relationship_id(relationship_id)
            claimed.add(inverse_by_id[relationship_id])
            continue
        if candidate in graph_relationship_namespace_by_id:
            if candidates.get(candidate) == relationship_id:
                inverse_by_id[relationship_id] = candidate
                continue
            logger.warning(
                "Inverse %r of graph relationship %r declares its own reverse; qualifying the inverse id",
                candidate,
                relationship_id,
            )
            raw_name = graph_relationship_display_name_by_id.get(candidate, candidate)
        else:
            if candidate not in claimed:
                inverse_by_id[relationship_id] = candidate
                claimed.add(candidate)
                continue
            raw_name = raw_inverse_names.get(relationship_id, candidate)
        forward_namespace = namespace_by_relationship.get(relationship_id)
        qualified = f"{forward_namespace}:{raw_name}" if forward_namespace is not None else None
        if qualified is not None and qualified not in claimed:
            inverse_by_id[relationship_id] = qualified
            claimed.add(qualified)
            continue
        synthetic = _synthetic_inverse_relationship_id(relationship_id)
        logger.warning(
            "No unique inverse relationship for %r (inverse %r already claimed); using %r",
            relationship_id,
            candidate,
            synthetic,
        )
        inverse_by_id[relationship_id] = synthetic
        claimed.add(synthetic)
    return inverse_by_id


def _synthetic_inverse_relationship_id(relationship_id: str) -> str:
    """Deterministic unique inverse id, used when the address space publishes none."""
    return f"inverseOf_{relationship_id}"
