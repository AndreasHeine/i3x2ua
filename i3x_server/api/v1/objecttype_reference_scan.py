"""Which type element IDs the built i3X model references.

Scans the model's object and property planes so the object type context can serve exactly
the types instances and properties need, including unresolved ones.
"""

from __future__ import annotations

from i3x_server.api.v1.object_helpers import _unknown_type_element_id
from i3x_server.application.ports.opcua import OpcUaNamespaceInfo
from i3x_server.domain.utils import expanded_node_id, is_null_opcua_type_node_id
from i3x_server.schemas.state import BuildResult


def _collect_referenced_type_element_ids(
    model: BuildResult,
    namespace_infos: list[OpcUaNamespaceInfo],
    object_type_element_ids_by_node_id: dict[str, str],
) -> set[str]:
    referenced: set[str] = set()
    for node in model.nodes_by_id.values():
        if node.kind == "property":
            raw_type_element_id = node.type or "unknown-type"
            type_element_id = expanded_node_id(raw_type_element_id, namespace_infos)
            if is_null_opcua_type_node_id(type_element_id):
                referenced.add(_unknown_type_element_id(namespace_infos))
                continue
        else:
            source_type_id = node.source_type_id
            if not source_type_id:
                referenced.add(_unknown_type_element_id(namespace_infos))
                continue
            source_type_id_expanded = expanded_node_id(source_type_id, namespace_infos)
            type_element_id = object_type_element_ids_by_node_id.get(source_type_id, source_type_id_expanded)
        if is_null_opcua_type_node_id(type_element_id):
            continue
        referenced.add(type_element_id)
    return referenced


def _collect_property_type_element_ids(
    model: BuildResult,
    namespace_infos: list[OpcUaNamespaceInfo],
) -> set[str]:
    property_type_ids: set[str] = set()
    for node in model.nodes_by_id.values():
        if node.kind != "property":
            continue
        raw_type = node.type or ""
        expanded = expanded_node_id(raw_type, namespace_infos)
        if not expanded or is_null_opcua_type_node_id(expanded):
            continue
        property_type_ids.add(expanded)
    return property_type_ids
