"""Object type response construction for type IDs the server did not list.

Unresolved source type IDs are classified into a served ``ObjectTypeResponse`` in a fixed
order: a resolved custom DataType hierarchy first, then an opaque DataType fallback for
property types, then a generic inference (optionally resolving the live BrowseName), and
finally a placeholder. Every path stamps a virtual element id derived from the namespace.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from i3x_server.api.v1 import objecttype_runtime_flags
from i3x_server.api.v1.contracts import ObjectTypeResponse
from i3x_server.api.v1.object_helpers import (
    _namespace_version_for_uri,
    _server_uri_for_namespace_infos,
    _virtual_object_type_element_id,
)
from i3x_server.api.v1.objecttype_schema_utils import (
    _array_items_schema_for_builtin_data_type,
    _collect_transitive_defs,
    _wrapped_value_schema,
)
from i3x_server.api.v1.objecttype_ua_names import (
    _ABSTRACT_BUILTIN_DATATYPE_NAMES,
    _INTEGER_RANGES_BY_BUILTIN_ID,
    _UA_BUILTIN_DATATYPE_NAMES,
    _UA_STANDARD_INTEGER_OPTIONSET_TYPE_NAMES,
    _UA_STANDARD_NON_DATATYPE_TYPE_NAMES,
    _UA_STANDARD_OBJECT_FALLBACK_TYPE_NAMES,
    _is_builtin_ua_datatype_node_id,
    _is_standard_ua_namespace_node_id,
    _standard_ua_type_name,
)
from i3x_server.application.ports.opcua import OpcUaClientProtocol, OpcUaNamespaceInfo
from i3x_server.domain.utils import (
    I3X_UNKNOWN_NAMESPACE_URI,
    canonical_namespace_uri,
    expanded_node_id,
    namespace_uri_for_node_id,
    namespace_uri_from_expanded_node_id,
)
from i3x_server.schemas.objecttype_schema import (
    build_data_type_schema,
    json_schema_for_opcua_type,
    remove_opcua_schema_fields,
)

logger = logging.getLogger(__name__)


def _virtual_element_id(
    namespace_uri: str,
    display_name: str,
    source_type_id: str,
    namespace_infos: list[OpcUaNamespaceInfo],
) -> str:
    return _virtual_object_type_element_id(
        namespace_uri,
        display_name,
        source_type_id,
        _server_uri_for_namespace_infos(namespace_infos),
        _namespace_version_for_uri(namespace_uri, namespace_infos),
    )


def _unknown_type_placeholder(
    element_id: str,
    namespace_infos: list[OpcUaNamespaceInfo],
) -> ObjectTypeResponse:
    namespace_uri = namespace_uri_from_expanded_node_id(element_id)
    if namespace_uri is None:
        namespace_uri = namespace_uri_for_node_id(element_id, namespace_infos)
    if not namespace_uri:
        namespace_uri = namespace_infos[0].uri if namespace_infos else I3X_UNKNOWN_NAMESPACE_URI
    namespace_uri = canonical_namespace_uri(namespace_uri, namespace_infos) if namespace_infos else namespace_uri
    return ObjectTypeResponse(
        elementId=element_id,
        displayName="UnknownType",
        namespaceUri=namespace_uri,
        sourceTypeId=element_id,
        schema={
            "title": "UnknownType",
            "description": "Placeholder type generated for unresolved source type IDs",
        },
    )


def _synthetic_object_types_from_structure_defs(
    listed_object_types: list[ObjectTypeResponse],
    namespace_infos: list[OpcUaNamespaceInfo],
) -> list[ObjectTypeResponse]:
    synthetic_by_source_type_id: dict[str, ObjectTypeResponse] = {}
    for item in listed_object_types:
        defs = item.schema_.get("$defs") if isinstance(item.schema_, Mapping) else None
        if not isinstance(defs, Mapping):
            continue

        for raw_def in defs.values():
            if not isinstance(raw_def, Mapping):
                continue

            source_hint = raw_def.get("x-opcua-structureDataType") or raw_def.get("x-opcua-structureTypeId")
            if not isinstance(source_hint, str) or not source_hint:
                continue

            source_type_id = expanded_node_id(source_hint, namespace_infos)
            source_key = source_type_id.lower()
            if source_key in synthetic_by_source_type_id:
                continue

            namespace_uri = namespace_uri_from_expanded_node_id(source_type_id)
            if namespace_uri is None:
                namespace_uri = namespace_uri_for_node_id(source_type_id, namespace_infos)
            if not namespace_uri:
                namespace_uri = item.namespaceUri
            namespace_uri = canonical_namespace_uri(namespace_uri, namespace_infos)

            display_name_raw = raw_def.get("title")
            display_name = (
                display_name_raw if isinstance(display_name_raw, str) and display_name_raw else "StructureType"
            )
            schema = deepcopy(dict(raw_def))
            required_defs = _collect_transitive_defs(schema, defs)
            if required_defs:
                schema["$defs"] = required_defs
            schema.setdefault("x-opcua-nodeId", source_type_id)
            schema.setdefault("x-opcua-displayName", display_name)
            schema = _wrapped_value_schema(schema, display_name=display_name, source_type_id=source_type_id)
            if not objecttype_runtime_flags._include_mcp_opcua_metadata():
                schema = remove_opcua_schema_fields(schema)

            synthetic_by_source_type_id[source_key] = ObjectTypeResponse(
                elementId=_virtual_element_id(namespace_uri, display_name, source_type_id, namespace_infos),
                displayName=display_name,
                namespaceUri=namespace_uri,
                sourceTypeId=source_type_id,
                schema=schema,
            )

    return list(synthetic_by_source_type_id.values())


def _scalar_schema_for_standard_ua_datatype_node_id(element_id: str) -> dict[str, Any] | None:
    if not _is_standard_ua_namespace_node_id(element_id):
        return None

    name = _standard_ua_type_name(element_id)
    if not name:
        return None

    if name in _UA_STANDARD_NON_DATATYPE_TYPE_NAMES:
        return None
    if name in _UA_STANDARD_INTEGER_OPTIONSET_TYPE_NAMES:
        return {"type": "integer"}
    if name in _UA_STANDARD_OBJECT_FALLBACK_TYPE_NAMES:
        return {"type": "object"}
    if name.endswith("DataType") or name.endswith("Type"):
        return {"type": "object"}
    if name.endswith("State") or name.endswith("Enumeration") or name.endswith("Enum"):
        return {"type": "integer"}
    if "_" in name:
        return None
    if name.endswith("ObjectType") or name.endswith("VariableType") or name.endswith("ReferenceType"):
        return None

    inferred = json_schema_for_opcua_type(name)
    if not inferred or inferred == {"type": "string"}:
        return None
    return inferred


def _datatype_object_type_from_source_type_id(
    source_type_id: str,
    namespace_infos: list[OpcUaNamespaceInfo],
    resolved_scalar_schema: Mapping[str, Any] | None = None,
    resolved_browse_name: str | None = None,
) -> ObjectTypeResponse | None:
    schema: Mapping[str, Any] | None = None
    if resolved_scalar_schema is not None:
        scalar_schema = dict(resolved_scalar_schema)
        schema = {
            "oneOf": [
                {"type": "null"},
                scalar_schema,
                {
                    "type": "array",
                    "items": _array_items_schema_for_builtin_data_type(source_type_id, scalar_schema),
                },
            ]
        }
    else:
        schema = build_data_type_schema(source_type_id, namespace_infos)

    if not isinstance(schema, Mapping):
        fallback_scalar_schema: dict[str, Any] | None = None
        if _is_builtin_ua_datatype_node_id(source_type_id):
            fallback_scalar_schema = json_schema_for_opcua_type(source_type_id)
        else:
            fallback_scalar_schema = _scalar_schema_for_standard_ua_datatype_node_id(source_type_id)
        if fallback_scalar_schema is None:
            return None
        schema = (
            {
                "oneOf": [
                    {"type": "null"},
                    dict(fallback_scalar_schema),
                    {
                        "type": "array",
                        "items": _array_items_schema_for_builtin_data_type(source_type_id, fallback_scalar_schema),
                    },
                ]
            }
            if fallback_scalar_schema
            else {}
        )

    node_id_match = re.match(r"^nsu=[^;]+;i=(\d+)$", source_type_id, flags=re.IGNORECASE)
    numeric_id = int(node_id_match.group(1)) if node_id_match is not None else None
    builtin_id = numeric_id if _is_builtin_ua_datatype_node_id(source_type_id) else None

    namespace_uri = namespace_uri_from_expanded_node_id(source_type_id)
    if namespace_uri is None:
        namespace_uri = namespace_uri_for_node_id(source_type_id, namespace_infos)
    if not namespace_uri:
        return None
    namespace_uri = canonical_namespace_uri(namespace_uri, namespace_infos)

    title = schema.get("title") if isinstance(schema, Mapping) else None
    display_name = title if isinstance(title, str) and title else "StructureType"
    if resolved_scalar_schema is not None and display_name == "StructureType":
        node_id_match = re.match(r"^nsu=[^;]+;i=(\d+)$", source_type_id, flags=re.IGNORECASE)
        if node_id_match is not None:
            display_name = f"DataType_i_{node_id_match.group(1)}"
    if resolved_browse_name:
        display_name = resolved_browse_name
    if builtin_id is not None:
        display_name = _UA_BUILTIN_DATATYPE_NAMES.get(builtin_id, display_name)
    elif _is_standard_ua_namespace_node_id(source_type_id):
        standard_name = _standard_ua_type_name(source_type_id)
        if standard_name:
            display_name = standard_name

    if "oneOf" not in schema:
        schema = _wrapped_value_schema(schema, display_name=display_name, source_type_id=source_type_id)

    schema_payload = dict(schema)
    schema_payload.setdefault("title", display_name)
    schema_payload.setdefault("x-opcua-nodeId", source_type_id)
    schema_payload.setdefault("x-opcua-displayName", display_name)
    if not objecttype_runtime_flags._include_mcp_opcua_metadata():
        schema_payload = remove_opcua_schema_fields(schema_payload)

    return ObjectTypeResponse(
        elementId=_virtual_element_id(namespace_uri, display_name, source_type_id, namespace_infos),
        displayName=display_name,
        namespaceUri=namespace_uri,
        sourceTypeId=source_type_id,
        schema=schema_payload,
    )


@dataclass(frozen=True, slots=True)
class _ResolvedCustomDataType:
    scalar_schema: dict[str, Any]
    browse_name: str | None


def _scalar_schema_from_type_hierarchy(
    hierarchy: list[Any],
    namespace_infos: list[OpcUaNamespaceInfo],
) -> dict[str, Any] | None:
    abstract_scalar_schema: dict[str, Any] | None = None
    for type_info in hierarchy[1:]:
        node_id = getattr(type_info, "node_id", None)
        if not isinstance(node_id, str):
            continue
        expanded_id = expanded_node_id(node_id, namespace_infos)
        if not _is_standard_ua_namespace_node_id(expanded_id):
            continue
        name = _standard_ua_type_name(expanded_id)
        if not name:
            continue
        schema = json_schema_for_opcua_type(expanded_id)
        if not schema:
            continue
        if name in _ABSTRACT_BUILTIN_DATATYPE_NAMES:
            if abstract_scalar_schema is None:
                abstract_scalar_schema = schema
            continue
        node_id_match = re.match(r"^nsu=[^;]+;i=(\d+)$", expanded_id, flags=re.IGNORECASE)
        builtin_id = int(node_id_match.group(1)) if node_id_match else None
        integer_range = _INTEGER_RANGES_BY_BUILTIN_ID.get(builtin_id) if builtin_id is not None else None
        if integer_range is not None:
            schema["minimum"], schema["maximum"] = integer_range
        return schema
    return abstract_scalar_schema


def _indexed_node_id_for_hierarchy_lookup(
    expanded_node_id: str,
    namespace_infos: list[OpcUaNamespaceInfo],
) -> str:
    match = re.match(r"^nsu=([^;]+);(.+)$", expanded_node_id, flags=re.IGNORECASE)
    if match is None:
        return expanded_node_id
    namespace_uri = canonical_namespace_uri(match.group(1), namespace_infos)
    for namespace_index, namespace_info in enumerate(namespace_infos):
        if namespace_info.uri == namespace_uri:
            return f"ns={namespace_index};{match.group(2)}"
    return expanded_node_id


async def _resolve_custom_scalar_datatype_schemas(
    source_type_ids: set[str],
    namespace_infos: list[OpcUaNamespaceInfo],
    opcua_client: OpcUaClientProtocol,
) -> dict[str, _ResolvedCustomDataType]:
    candidates_set: set[str] = set()
    for type_id in source_type_ids:
        if not type_id:
            continue
        expanded_type_id = expanded_node_id(type_id, namespace_infos)
        if not re.match(r"^(?:nsu=[^;]+;|ns=\d+;)?[isgb]=.+$", expanded_type_id, flags=re.IGNORECASE):
            continue
        if not _is_standard_ua_namespace_node_id(expanded_type_id):
            candidates_set.add(expanded_type_id)
    candidates = sorted(candidates_set)
    if not candidates:
        return {}

    lookup_ids = {_indexed_node_id_for_hierarchy_lookup(type_id, namespace_infos): type_id for type_id in candidates}
    resolve_many = getattr(opcua_client, "resolve_type_definition_hierarchies", None)
    resolve_one = getattr(opcua_client, "resolve_type_definition_hierarchy", None)
    if not callable(resolve_many) and not callable(resolve_one):
        return {}

    hierarchies: dict[str, list[Any]] = {}
    if callable(resolve_many):
        try:
            resolved_hierarchies = await resolve_many(sorted(lookup_ids))
            if isinstance(resolved_hierarchies, Mapping):
                for lookup_id, hierarchy in resolved_hierarchies.items():
                    candidate = lookup_ids.get(lookup_id) if isinstance(lookup_id, str) else None
                    if candidate is not None and isinstance(hierarchy, list):
                        hierarchies[candidate] = hierarchy
        except Exception:
            logger.warning("Custom DataType hierarchy batch resolution failed", exc_info=True)
    if callable(resolve_one):
        for type_id in candidates:
            if type_id in hierarchies:
                continue
            try:
                lookup_id = _indexed_node_id_for_hierarchy_lookup(type_id, namespace_infos)
                hierarchies[type_id] = await resolve_one(lookup_id)
            except Exception:
                logger.warning("Custom DataType hierarchy resolution failed for %s", type_id, exc_info=True)

    resolved: dict[str, _ResolvedCustomDataType] = {}
    for type_id in candidates:
        hierarchy = hierarchies.get(type_id)
        if not isinstance(hierarchy, list):
            continue
        scalar_schema = _scalar_schema_from_type_hierarchy(hierarchy, namespace_infos)
        if scalar_schema is not None:
            browse_name: str | None = None
            if hierarchy:
                root_type = hierarchy[0]
                root_node_id = getattr(root_type, "node_id", None)
                root_browse_name = getattr(root_type, "browse_name", None)
                if (
                    isinstance(root_node_id, str)
                    and expanded_node_id(root_node_id, namespace_infos).lower() == type_id.lower()
                    and isinstance(root_browse_name, str)
                    and root_browse_name.strip()
                ):
                    browse_name = root_browse_name.strip()
            resolved[type_id.lower()] = _ResolvedCustomDataType(scalar_schema, browse_name)
    return resolved


def _opaque_datatype_object_type_from_source_type_id(
    source_type_id: str,
    namespace_infos: list[OpcUaNamespaceInfo],
) -> ObjectTypeResponse | None:
    namespace_uri = namespace_uri_from_expanded_node_id(source_type_id)
    if namespace_uri is None:
        namespace_uri = namespace_uri_for_node_id(source_type_id, namespace_infos)
    if not namespace_uri:
        return None
    namespace_uri = canonical_namespace_uri(namespace_uri, namespace_infos)

    display_name = _standard_ua_type_name(source_type_id)
    if not display_name:
        node_id_match = re.match(r"^nsu=[^;]+;i=(\d+)$", source_type_id, flags=re.IGNORECASE)
        if node_id_match is not None:
            display_name = f"DataType_i_{node_id_match.group(1)}"
        else:
            display_name = "DataType"

    schema_payload = {
        "title": display_name,
        "description": "Fallback schema for unresolved OPC UA DataType",
        "oneOf": [
            {"type": "object"},
            {"type": "array", "items": {"type": "object"}},
            {"type": "number"},
            {"type": "string"},
            {"type": "boolean"},
        ],
        "x-opcua-nodeId": source_type_id,
        "x-opcua-displayName": display_name,
    }
    if not objecttype_runtime_flags._include_mcp_opcua_metadata():
        schema_payload = remove_opcua_schema_fields(schema_payload)

    return ObjectTypeResponse(
        elementId=_virtual_element_id(namespace_uri, display_name, source_type_id, namespace_infos),
        displayName=display_name,
        namespaceUri=namespace_uri,
        sourceTypeId=source_type_id,
        schema=schema_payload,
    )


async def _generic_object_type_from_source_type_id(
    source_type_id: str,
    namespace_infos: list[OpcUaNamespaceInfo],
    opcua_client: OpcUaClientProtocol,
    browse_name_cache: dict[str, str | None],
    lookup_budget: dict[str, int],
) -> ObjectTypeResponse | None:
    expanded_match = re.match(r"^nsu=([^;]+);([isgb])=(.+)$", source_type_id, flags=re.IGNORECASE)
    if expanded_match is None:
        return None

    namespace_uri = expanded_match.group(1)
    identifier_kind = expanded_match.group(2).lower()
    identifier_value = expanded_match.group(3)

    display_name: str | None = None
    if identifier_kind == "i":
        standard_name = _standard_ua_type_name(source_type_id)
        if standard_name and "_" not in standard_name:
            display_name = standard_name
    elif identifier_kind == "s":
        token = identifier_value.rsplit("/", 1)[-1].rsplit(".", 1)[-1].strip()
        if token:
            display_name = token

    if not display_name:
        display_name = f"InferredType_{identifier_kind}_{identifier_value}"

    if (
        objecttype_runtime_flags._live_type_name_lookup_enabled()
        and display_name.startswith("InferredType_")
        and identifier_kind == "i"
        and lookup_budget.get("remaining", 0) > 0
    ):
        lookup_node_id = source_type_id
        if _is_standard_ua_namespace_node_id(source_type_id):
            lookup_node_id = f"ns=0;i={identifier_value}"
        resolved_name = browse_name_cache.get(lookup_node_id)
        if resolved_name is None:
            browse_name_reader = getattr(opcua_client, "read_browse_name", None)
            if callable(browse_name_reader):
                lookup_budget["remaining"] = max(0, lookup_budget.get("remaining", 0) - 1)
                try:
                    resolved_name = await asyncio.wait_for(
                        browse_name_reader(lookup_node_id),
                        timeout=objecttype_runtime_flags._live_type_name_lookup_timeout_seconds(),
                    )
                except Exception:
                    resolved_name = None
            browse_name_cache[lookup_node_id] = resolved_name
        if isinstance(resolved_name, str) and resolved_name.strip():
            display_name = resolved_name.strip()

    canonical_uri = canonical_namespace_uri(namespace_uri, namespace_infos)
    schema_payload = {
        "type": "object",
        "title": display_name,
        "description": "Generic placeholder schema inferred from source type ID",
        "x-opcua-nodeId": source_type_id,
        "x-opcua-displayName": display_name,
    }
    if not objecttype_runtime_flags._include_mcp_opcua_metadata():
        schema_payload = remove_opcua_schema_fields(schema_payload)
    return ObjectTypeResponse(
        elementId=_virtual_element_id(canonical_uri, display_name, source_type_id, namespace_infos),
        displayName=display_name,
        namespaceUri=canonical_uri,
        sourceTypeId=source_type_id,
        schema=schema_payload,
    )
