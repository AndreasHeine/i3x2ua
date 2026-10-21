from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from contextlib import nullcontext as _nullcontext
from copy import deepcopy
from dataclasses import dataclass
from time import perf_counter
from types import SimpleNamespace
from typing import Any

from asyncua import ua

from i3x_server.api.v1.contracts import ObjectTypeResponse
from i3x_server.api.v1.object_helpers import (
    _canonical_namespace_uri,
    _expanded_node_id,
    _is_null_opcua_type_node_id,
    _namespace_uri_for_node_id,
    _namespace_uri_from_expanded_node_id,
    _namespace_version_for_uri,
    _object_type_element_ids_by_node_id,
    _server_uri_for_namespace_infos,
    _to_object_type,
    _unknown_type_element_id,
    _virtual_object_type_element_id,
)
from i3x_server.application.ports.opcua import OpcUaClientProtocol, OpcUaNamespaceInfo, OpcUaObjectTypeInfo
from i3x_server.config.settings import Settings, get_settings
from i3x_server.errors import i3x_http_error
from i3x_server.http_caching import (
    NAMESPACES_PART,
    OBJECT_TYPES_PART,
    content_revision,
    mark_not_cacheable,
    record_revision,
)
from i3x_server.model.revision import model_revision
from i3x_server.schemas.objecttype_schema import (
    build_data_type_schema,
    deduplicate_schema_fragments,
    json_schema_for_opcua_type,
    remove_opcua_schema_fields,
)
from i3x_server.schemas.state import BuildResult

logger = logging.getLogger(__name__)

_UA_BUILTIN_DATATYPE_NAMES: dict[int, str] = {
    1: "Boolean",
    2: "SByte",
    3: "Byte",
    4: "Int16",
    5: "UInt16",
    6: "Int32",
    7: "UInt32",
    8: "Int64",
    9: "UInt64",
    10: "Float",
    11: "Double",
    12: "String",
    13: "DateTime",
    14: "Guid",
    15: "ByteString",
    16: "XmlElement",
    17: "NodeId",
    18: "ExpandedNodeId",
    19: "StatusCode",
    20: "QualifiedName",
    21: "LocalizedText",
    22: "ExtensionObject",
    23: "DataValue",
    24: "Variant",
    25: "DiagnosticInfo",
}

_NULLABLE_BUILTIN_SCALAR_IDS = {12, 15, 16}
_WRAPPER_METADATA_KEYS = {
    "$defs",
    "description",
    "title",
    "x-opcua-displayName",
    "x-opcua-nodeId",
    "x-opcua-structureDataType",
    "x-opcua-structureTypeId",
}


def _array_items_schema_for_builtin_data_type(
    source_type_id: str,
    scalar_schema: Mapping[str, Any],
) -> dict[str, Any]:
    node_id_match = re.match(r"^nsu=[^;]+;i=(\d+)$", source_type_id, flags=re.IGNORECASE)
    builtin_id = int(node_id_match.group(1)) if node_id_match is not None else None
    items_schema = dict(scalar_schema)
    if builtin_id not in _NULLABLE_BUILTIN_SCALAR_IDS:
        return items_schema

    schema_type = items_schema.get("type")
    if schema_type == "string":
        items_schema["type"] = ["string", "null"]
    return items_schema


def _wrapped_value_schema(
    schema: Mapping[str, Any],
    *,
    display_name: str,
    source_type_id: str,
) -> dict[str, Any]:
    wrapper: dict[str, Any] = {
        "title": display_name,
        "x-opcua-nodeId": source_type_id,
        "x-opcua-displayName": display_name,
    }

    for key in ("description", "x-opcua-structureDataType", "x-opcua-structureTypeId"):
        value = schema.get(key)
        if value is not None:
            wrapper[key] = value

    defs = deepcopy(dict(schema.get("$defs", {}))) if isinstance(schema.get("$defs"), Mapping) else {}

    value_schema = deepcopy(dict(schema))
    for key in _WRAPPER_METADATA_KEYS:
        value_schema.pop(key, None)

    if not value_schema:
        return wrapper

    value_def_key_base = re.sub(r"[^0-9A-Za-z]+", "", display_name) or "Value"
    value_def_key = f"{value_def_key_base}Value"
    suffix = 2
    while value_def_key in defs:
        value_def_key = f"{value_def_key_base}Value{suffix}"
        suffix += 1
    defs[value_def_key] = value_schema
    wrapper["$defs"] = defs

    value_ref = {"$ref": f"#/$defs/{value_def_key}"}

    wrapper["oneOf"] = [
        {"type": "null"},
        value_ref,
        {"type": "array", "items": deepcopy(value_ref)},
    ]
    return wrapper


_UA_STANDARD_NON_DATATYPE_TYPE_NAMES: set[str] = {
    "BaseObjectType",
    "FolderType",
    "BaseVariableType",
    "BaseDataVariableType",
    "PropertyType",
    "DataTypeDescriptionType",
    "DataTypeDictionaryType",
    "DataTypeSystemType",
    "DataTypeEncodingType",
    "ModellingRuleType",
    "NamingRuleType",
}

_UA_STANDARD_INTEGER_OPTIONSET_TYPE_NAMES: set[str] = {
    "PermissionType",
    "AccessRestrictionType",
}

_UA_STANDARD_OBJECT_FALLBACK_TYPE_NAMES: set[str] = {
    "Range",
    "EUInformation",
    "Annotation",
    "RolePermissionType",
}


def _runtime_settings() -> Settings:
    return get_settings()


_ENABLE_LIVE_TYPE_NAME_LOOKUP: bool = _runtime_settings().enable_type_browsename_lookup
_LIVE_TYPE_NAME_LOOKUP_TIMEOUT_S: float = _runtime_settings().type_browsename_lookup_timeout_s
_LIVE_TYPE_NAME_LOOKUP_MAX_PER_REQUEST: int = _runtime_settings().type_browsename_lookup_max


def _live_type_name_lookup_enabled() -> bool:
    return _ENABLE_LIVE_TYPE_NAME_LOOKUP


def _live_type_name_lookup_timeout_seconds() -> float:
    return _LIVE_TYPE_NAME_LOOKUP_TIMEOUT_S


def _live_type_name_lookup_max_per_request() -> int:
    return _LIVE_TYPE_NAME_LOOKUP_MAX_PER_REQUEST


def _include_mcp_opcua_metadata() -> bool:
    return _runtime_settings().mcp_include_opcua_metadata


@dataclass(slots=True)
class _ObjectTypeContext:
    namespace_infos: list[OpcUaNamespaceInfo]
    object_types: list[OpcUaObjectTypeInfo]
    element_ids_by_node_id: dict[str, str]
    items: list[ObjectTypeResponse]
    source_type_to_element_id: dict[str, str]
    # Revision of the served content, so equal tags always mean equal representations.
    revision: str = ""


_OBJECT_TYPE_KEY_FIELDS = ("node_id", "parent_node_id", "browse_name", "display_name", "description", "is_abstract")
# Member values are arbitrary OPC UA objects whose repr may not be stable, so only
# structural attributes identify a definition; value changes still reach the served revision.
_OBJECT_TYPE_MEMBER_KEY_FIELDS = (
    "node_id",
    "browse_name",
    "display_name",
    "description",
    "node_class",
    "data_type",
    "modelling_rule",
    "variant_type",
    "is_array",
    "value_rank",
    "array_dimensions",
    "parent_node_id",
    "type_definition_id",
    "reference_type_id",
    "reference_type",
    "reference_order",
)


def _object_types_token(object_types: list[OpcUaObjectTypeInfo]) -> str:
    return content_revision(
        (
            tuple(getattr(item, name, None) for name in _OBJECT_TYPE_KEY_FIELDS),
            tuple(sorted((getattr(item, "properties", None) or {}).items())),
            tuple(
                tuple(getattr(member, name, None) for name in _OBJECT_TYPE_MEMBER_KEY_FIELDS)
                for member in getattr(item, "members", None) or []
            ),
        )
        for item in object_types
    )


def _memoized_object_types_token(request: Any, object_types: list[OpcUaObjectTypeInfo]) -> str:
    # The OPC UA client serves the same cached list until its metadata reload replaces it,
    # so hashing every definition once per list keeps this off the per-request path.
    # Holding the list in the memo keeps its id from being reused by another object.
    state = request.app.state
    memo = getattr(state, "object_types_token_memo", None)
    if isinstance(memo, tuple) and len(memo) == 2 and memo[0] is object_types:
        return str(memo[1])
    token = _object_types_token(object_types)
    state.object_types_token_memo = (object_types, token)
    return token


def _object_type_context_revision(context: _ObjectTypeContext) -> str:
    return content_revision(
        (
            tuple(
                (info.uri, info.display_name, info.namespace_version, info.server_uri)
                for info in context.namespace_infos
            ),
            tuple(item.model_dump_json() for item in context.items),
            tuple(sorted(context.element_ids_by_node_id.items())),
            tuple(sorted(context.source_type_to_element_id.items())),
        )
    )


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
    namespace_uri = _namespace_uri_from_expanded_node_id(element_id)
    if namespace_uri is None:
        namespace_uri = _namespace_uri_for_node_id(element_id, namespace_infos)
    if not namespace_uri:
        namespace_uri = namespace_infos[0].uri if namespace_infos else "https://cesmii.org/i3x/unknown"
    namespace_uri = _canonical_namespace_uri(namespace_uri, namespace_infos) if namespace_infos else namespace_uri
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


def _object_type_alias_with_element_id(item: ObjectTypeResponse, element_id: str) -> ObjectTypeResponse:
    return ObjectTypeResponse(
        elementId=element_id,
        displayName=item.displayName,
        namespaceUri=item.namespaceUri,
        sourceTypeId=item.sourceTypeId,
        schema=deepcopy(item.schema_),
        related=deepcopy(item.related),
    )


def _is_builtin_ua_datatype_node_id(element_id: str) -> bool:
    match = re.match(r"^nsu=([^;]+);i=(\d+)$", element_id, flags=re.IGNORECASE)
    if match is None:
        return False

    namespace_uri = match.group(1).rstrip("/").lower()
    if namespace_uri != "http://opcfoundation.org/ua":
        return False

    identifier = int(match.group(2))
    return 1 <= identifier <= 25


def _is_standard_ua_namespace_node_id(element_id: str) -> bool:
    expanded_match = re.match(r"^nsu=([^;]+);", element_id, flags=re.IGNORECASE)
    if expanded_match is not None:
        namespace_uri = expanded_match.group(1).rstrip("/").lower()
        return namespace_uri == "http://opcfoundation.org/ua"

    indexed_match = re.match(r"^ns=(\d+);", element_id, flags=re.IGNORECASE)
    if indexed_match is None:
        return False
    return int(indexed_match.group(1)) == 0


def _collect_referenced_type_element_ids(
    model: BuildResult,
    namespace_infos: list[OpcUaNamespaceInfo],
    object_type_element_ids_by_node_id: dict[str, str],
) -> set[str]:
    referenced: set[str] = set()
    for node in model.nodes_by_id.values():
        if node.kind == "property":
            raw_type_element_id = node.type or "unknown-type"
            type_element_id = _expanded_node_id(raw_type_element_id, namespace_infos)
            if _is_null_opcua_type_node_id(type_element_id):
                referenced.add(_unknown_type_element_id(namespace_infos))
                continue
        else:
            source_type_id = node.source_type_id
            if not source_type_id:
                referenced.add(_unknown_type_element_id(namespace_infos))
                continue
            source_type_id_expanded = _expanded_node_id(source_type_id, namespace_infos)
            type_element_id = object_type_element_ids_by_node_id.get(source_type_id, source_type_id_expanded)
        if _is_null_opcua_type_node_id(type_element_id):
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
        expanded = _expanded_node_id(raw_type, namespace_infos)
        if not expanded or _is_null_opcua_type_node_id(expanded):
            continue
        property_type_ids.add(expanded)
    return property_type_ids


def _iter_local_defs_refs(value: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(value, Mapping):
        ref_value = value.get("$ref")
        if isinstance(ref_value, str) and ref_value.startswith("#/$defs/"):
            refs.append(ref_value.split("#/$defs/", 1)[1])
        for nested in value.values():
            refs.extend(_iter_local_defs_refs(nested))
        return refs

    if isinstance(value, list):
        for nested in value:
            refs.extend(_iter_local_defs_refs(nested))
    return refs


def _collect_transitive_defs(schema: Mapping[str, Any], defs: Mapping[str, Any]) -> dict[str, Any]:
    collected: dict[str, Any] = {}
    pending = _iter_local_defs_refs(schema)
    seen: set[str] = set()
    while pending:
        def_name = pending.pop()
        if def_name in seen:
            continue
        seen.add(def_name)
        referenced = defs.get(def_name)
        if not isinstance(referenced, Mapping):
            continue
        referenced_copy = deepcopy(dict(referenced))
        collected[def_name] = referenced_copy
        pending.extend(_iter_local_defs_refs(referenced_copy))
    return collected


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

            source_type_id = _expanded_node_id(source_hint, namespace_infos)
            source_key = source_type_id.lower()
            if source_key in synthetic_by_source_type_id:
                continue

            namespace_uri = _namespace_uri_from_expanded_node_id(source_type_id)
            if namespace_uri is None:
                namespace_uri = _namespace_uri_for_node_id(source_type_id, namespace_infos)
            if not namespace_uri:
                namespace_uri = item.namespaceUri
            namespace_uri = _canonical_namespace_uri(namespace_uri, namespace_infos)

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
            if not _include_mcp_opcua_metadata():
                schema = remove_opcua_schema_fields(schema)

            synthetic_by_source_type_id[source_key] = ObjectTypeResponse(
                elementId=_virtual_element_id(namespace_uri, display_name, source_type_id, namespace_infos),
                displayName=display_name,
                namespaceUri=namespace_uri,
                sourceTypeId=source_type_id,
                schema=schema,
            )

    return list(synthetic_by_source_type_id.values())


def _standard_ua_type_name(element_id: str) -> str | None:
    match = re.match(r"^nsu=[^;]+;i=(\d+)$", element_id, flags=re.IGNORECASE)
    if match is None:
        return None
    identifier = int(match.group(1))
    object_id_names = getattr(ua, "ObjectIdNames", None)
    if not isinstance(object_id_names, Mapping):
        return None
    candidate = object_id_names.get(identifier)
    return candidate if isinstance(candidate, str) and candidate else None


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

    namespace_uri = _namespace_uri_from_expanded_node_id(source_type_id)
    if namespace_uri is None:
        namespace_uri = _namespace_uri_for_node_id(source_type_id, namespace_infos)
    if not namespace_uri:
        return None
    namespace_uri = _canonical_namespace_uri(namespace_uri, namespace_infos)

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
    if not _include_mcp_opcua_metadata():
        schema_payload = remove_opcua_schema_fields(schema_payload)

    return ObjectTypeResponse(
        elementId=_virtual_element_id(namespace_uri, display_name, source_type_id, namespace_infos),
        displayName=display_name,
        namespaceUri=namespace_uri,
        sourceTypeId=source_type_id,
        schema=schema_payload,
    )


_ABSTRACT_BUILTIN_DATATYPE_NAMES = {"BaseDataType", "Number", "Integer", "UInteger", "Enumeration"}
_INTEGER_RANGES_BY_BUILTIN_ID: dict[int, tuple[int, int]] = {
    2: (-(2**7), 2**7 - 1),
    3: (0, 2**8 - 1),
    4: (-(2**15), 2**15 - 1),
    5: (0, 2**16 - 1),
    6: (-(2**31), 2**31 - 1),
    7: (0, 2**32 - 1),
    8: (-(2**63), 2**63 - 1),
    9: (0, 2**64 - 1),
}


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
        expanded_node_id = _expanded_node_id(node_id, namespace_infos)
        if not _is_standard_ua_namespace_node_id(expanded_node_id):
            continue
        name = _standard_ua_type_name(expanded_node_id)
        if not name:
            continue
        schema = json_schema_for_opcua_type(expanded_node_id)
        if not schema:
            continue
        if name in _ABSTRACT_BUILTIN_DATATYPE_NAMES:
            if abstract_scalar_schema is None:
                abstract_scalar_schema = schema
            continue
        node_id_match = re.match(r"^nsu=[^;]+;i=(\d+)$", expanded_node_id, flags=re.IGNORECASE)
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
    namespace_uri = _canonical_namespace_uri(match.group(1), namespace_infos)
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
        expanded_type_id = _expanded_node_id(type_id, namespace_infos)
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
                    and _expanded_node_id(root_node_id, namespace_infos).lower() == type_id.lower()
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
    namespace_uri = _namespace_uri_from_expanded_node_id(source_type_id)
    if namespace_uri is None:
        namespace_uri = _namespace_uri_for_node_id(source_type_id, namespace_infos)
    if not namespace_uri:
        return None
    namespace_uri = _canonical_namespace_uri(namespace_uri, namespace_infos)

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
    if not _include_mcp_opcua_metadata():
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
        _live_type_name_lookup_enabled()
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
                        timeout=_live_type_name_lookup_timeout_seconds(),
                    )
                except Exception:
                    resolved_name = None
            browse_name_cache[lookup_node_id] = resolved_name
        if isinstance(resolved_name, str) and resolved_name.strip():
            display_name = resolved_name.strip()

    canonical_namespace_uri = _canonical_namespace_uri(namespace_uri, namespace_infos)
    schema_payload = {
        "type": "object",
        "title": display_name,
        "description": "Generic placeholder schema inferred from source type ID",
        "x-opcua-nodeId": source_type_id,
        "x-opcua-displayName": display_name,
    }
    if not _include_mcp_opcua_metadata():
        schema_payload = remove_opcua_schema_fields(schema_payload)
    return ObjectTypeResponse(
        elementId=_virtual_element_id(canonical_namespace_uri, display_name, source_type_id, namespace_infos),
        displayName=display_name,
        namespaceUri=canonical_namespace_uri,
        sourceTypeId=source_type_id,
        schema=schema_payload,
    )


async def _build_object_type_context(
    model: BuildResult,
    namespace_infos: list[OpcUaNamespaceInfo],
    opcua_client: OpcUaClientProtocol,
    object_types: list[OpcUaObjectTypeInfo],
) -> _ObjectTypeContext:
    started = perf_counter()
    object_types_by_node_id = {item.node_id: item for item in object_types}
    element_ids_by_node_id = _object_type_element_ids_by_node_id(object_types, namespace_infos)
    property_type_element_ids = _collect_property_type_element_ids(model, namespace_infos)
    property_type_element_ids.update(
        _expanded_node_id(member.data_type, namespace_infos)
        for item in object_types
        for member in item.members
        if member.node_class.lower() == "variable" and member.data_type
    )
    resolved_custom_datatypes = await _resolve_custom_scalar_datatype_schemas(
        property_type_element_ids,
        namespace_infos,
        opcua_client,
    )
    resolved_data_type_schemas = {
        type_id: resolved.scalar_schema for type_id, resolved in resolved_custom_datatypes.items()
    }

    items: list[ObjectTypeResponse] = []
    chunk_size = 50
    for i in range(0, len(object_types), chunk_size):
        chunk = object_types[i : i + chunk_size]
        items.extend(
            [
                _to_object_type(
                    item,
                    model,
                    namespace_infos,
                    object_types_by_node_id,
                    element_ids_by_node_id,
                    {},
                    resolved_data_type_schemas,
                )
                for item in chunk
            ]
        )
        await asyncio.sleep(0)

    items.extend(_synthetic_object_types_from_structure_defs(items, namespace_infos))

    referenced_type_element_ids = _collect_referenced_type_element_ids(model, namespace_infos, element_ids_by_node_id)
    known_ids = {item.elementId for item in items}
    by_source_type_id: dict[str, ObjectTypeResponse] = {item.sourceTypeId.lower(): item for item in items}
    browse_name_cache: dict[str, str | None] = {}
    lookup_budget = {"remaining": _live_type_name_lookup_max_per_request()}

    for unresolved_id in sorted(referenced_type_element_ids - known_ids):
        unresolved_key = unresolved_id.lower()
        source_match = by_source_type_id.get(unresolved_key)
        if source_match is not None:
            continue

        resolved_datatype = resolved_custom_datatypes.get(unresolved_key)
        datatype_item = _datatype_object_type_from_source_type_id(
            unresolved_id,
            namespace_infos,
            resolved_datatype.scalar_schema if resolved_datatype is not None else None,
            resolved_datatype.browse_name if resolved_datatype is not None else None,
        )
        if datatype_item is not None:
            items.append(datatype_item)
            by_source_type_id[unresolved_key] = datatype_item
            continue

        if unresolved_id in property_type_element_ids:
            opaque_datatype_item = _opaque_datatype_object_type_from_source_type_id(unresolved_id, namespace_infos)
            if opaque_datatype_item is not None:
                items.append(opaque_datatype_item)
                by_source_type_id[unresolved_key] = opaque_datatype_item
                continue

        generic_item = await _generic_object_type_from_source_type_id(
            unresolved_id,
            namespace_infos,
            opcua_client,
            browse_name_cache,
            lookup_budget,
        )
        if generic_item is not None:
            items.append(generic_item)
            by_source_type_id[unresolved_key] = generic_item
            continue

        unknown_item = _unknown_type_placeholder(unresolved_id, namespace_infos)
        items.append(unknown_item)
        by_source_type_id[unresolved_key] = unknown_item

    canonical_items = [
        item.model_copy(update={"namespaceUri": _canonical_namespace_uri(item.namespaceUri, namespace_infos)})
        for item in items
    ]
    if not _include_mcp_opcua_metadata():
        canonical_items = [
            item.model_copy(update={"schema_": remove_opcua_schema_fields(item.schema_)}) for item in canonical_items
        ]
    schemas = deduplicate_schema_fragments(item.schema_ for item in canonical_items)
    for item, schema in zip(canonical_items, schemas, strict=True):
        item.schema_ = schema
    source_type_to_element_id = {key: item.elementId for key, item in by_source_type_id.items()}
    logger.debug(
        "Object type context built model_nodes=%d object_types=%d items=%d duration_s=%.3f",
        len(model.nodes_by_id),
        len(object_types),
        len(canonical_items),
        perf_counter() - started,
    )
    return _ObjectTypeContext(
        namespace_infos=namespace_infos,
        object_types=object_types,
        element_ids_by_node_id=element_ids_by_node_id,
        items=canonical_items,
        source_type_to_element_id=source_type_to_element_id,
    )


def _cached_object_type_context(
    request: Any,
    model_token: str,
    namespace_token: tuple[Any, ...],
    object_types_token: str,
) -> _ObjectTypeContext | None:
    cache = getattr(request.app.state, "object_type_context_cache", None)
    if not isinstance(cache, dict):
        return None
    cached_context = cache.get("context")
    if not isinstance(cached_context, _ObjectTypeContext):
        return None
    if (
        cache.get("model_token") != model_token
        or cache.get("namespace_token") != namespace_token
        or cache.get("object_types_token") != object_types_token
    ):
        return None
    return cached_context


def _stale_object_type_context(request: Any) -> _ObjectTypeContext | None:
    cache = getattr(request.app.state, "object_type_context_cache", None)
    if not isinstance(cache, dict):
        return None
    cached_context = cache.get("context")
    return cached_context if isinstance(cached_context, _ObjectTypeContext) else None


def _schedule_object_type_context_refresh(app: Any, model: BuildResult, opcua_client: OpcUaClientProtocol) -> None:
    task = getattr(app.state, "object_type_refresh_task", None)
    if isinstance(task, asyncio.Task) and not task.done():
        return
    app.state.object_type_refresh_task = asyncio.create_task(_refresh_object_type_context(app, model, opcua_client))


async def _refresh_object_type_context(app: Any, model: BuildResult, opcua_client: OpcUaClientProtocol) -> None:
    # Rebuild for the newest model: a request may still hold the model from before a refresh.
    current_model = getattr(app.state, "model_cache", None)
    target_model = current_model if isinstance(current_model, BuildResult) else model
    try:
        await _get_object_type_context(SimpleNamespace(app=app), target_model, opcua_client, allow_stale=False)
    except Exception:
        logger.exception("Background object type context refresh failed; serving previous context")


async def _get_object_type_context(
    request: Any,
    model: BuildResult,
    opcua_client: OpcUaClientProtocol,
    namespace_infos: list[OpcUaNamespaceInfo] | None = None,
    *,
    allow_stale: bool = True,
) -> _ObjectTypeContext:
    context = await _resolve_object_type_context(request, model, opcua_client, namespace_infos, allow_stale=allow_stale)
    # Record the context's own revision: a stale context must not be tagged as current.
    record_revision(request, OBJECT_TYPES_PART, context.revision)
    return context


async def _resolve_object_type_context(
    request: Any,
    model: BuildResult,
    opcua_client: OpcUaClientProtocol,
    namespace_infos: list[OpcUaNamespaceInfo] | None,
    *,
    allow_stale: bool,
) -> _ObjectTypeContext:
    started = perf_counter()
    resolved_namespace_infos = (
        namespace_infos if namespace_infos is not None else await opcua_client.get_namespace_infos()
    )
    object_types = await opcua_client.get_object_types()
    namespace_token = tuple(
        (info.uri, info.display_name, info.namespace_version, info.server_uri) for info in resolved_namespace_infos
    )
    # Full definitions, not just node ids: a changed type (same node id) must rebuild the context.
    object_types_token = _memoized_object_types_token(request, object_types)
    # Content revision, hashed once per build: equivalent rebuilds still hit the cache.
    model_token = model_revision(model)

    cached_context = _cached_object_type_context(request, model_token, namespace_token, object_types_token)
    if cached_context is not None:
        return cached_context

    if allow_stale:
        stale_context = _stale_object_type_context(request)
        if stale_context is not None:
            logger.warning("Serving stale object type context while it is rebuilt in the background")
            _schedule_object_type_context_refresh(request.app, model, opcua_client)
            return stale_context

    lock = getattr(request.app.state, "object_type_lock", None)
    async with lock if lock else _nullcontext():
        cached_context = _cached_object_type_context(request, model_token, namespace_token, object_types_token)
        if cached_context is not None:
            logger.debug(
                "Object type context cache hit model_nodes=%d object_types=%d duration_s=%.3f",
                len(model.nodes_by_id),
                len(object_types),
                perf_counter() - started,
            )
            return cached_context

        context = await _build_object_type_context(
            model=model,
            namespace_infos=resolved_namespace_infos,
            opcua_client=opcua_client,
            object_types=object_types,
        )
        # Hash the built output: it also covers live browse-name lookups that are not inputs.
        context.revision = _object_type_context_revision(context)
        request.app.state.object_type_context_cache = {
            "model_token": model_token,
            "namespace_token": namespace_token,
            "object_types_token": object_types_token,
            "context": context,
        }
        logger.info(
            "Object type context cache miss rebuilt model_nodes=%d object_types=%d items=%d duration_s=%.3f",
            len(model.nodes_by_id),
            len(object_types),
            len(context.items),
            perf_counter() - started,
        )
        return context


async def warm_object_type_context(
    app: Any,
    model: BuildResult,
    opcua_client: OpcUaClientProtocol,
) -> _ObjectTypeContext:
    """Populate the object type context cache outside of a request.

    Keeps the ~5s rebuild off the first request after startup and after each model refresh.
    """
    return await _get_object_type_context(SimpleNamespace(app=app), model, opcua_client, allow_stale=False)


async def _get_object_endpoint_context(
    request: Any,
    model: BuildResult,
    opcua_client: OpcUaClientProtocol,
) -> tuple[list[OpcUaNamespaceInfo], dict[str, str], dict[str, str]]:
    try:
        namespace_infos = await opcua_client.get_namespace_infos()
    except Exception as exc:
        raise i3x_http_error(
            502,
            "OpcUaNamespaceError",
            "Failed to read OPC UA namespaces",
            {"cause": str(exc)},
        ) from exc

    object_type_element_ids_by_node_id: dict[str, str] = {}
    object_type_element_ids_by_source_type: dict[str, str] = {}
    try:
        context = await _get_object_type_context(
            request,
            model,
            opcua_client,
            namespace_infos=namespace_infos,
        )
        object_type_element_ids_by_node_id = context.element_ids_by_node_id
        object_type_element_ids_by_source_type = context.source_type_to_element_id
    except Exception:
        object_type_element_ids_by_node_id = {}
        object_type_element_ids_by_source_type = {}
        mark_not_cacheable(request)

    record_revision(
        request,
        NAMESPACES_PART,
        content_revision(
            (info.uri, info.display_name, info.namespace_version, info.server_uri) for info in namespace_infos
        ),
    )
    return namespace_infos, object_type_element_ids_by_node_id, object_type_element_ids_by_source_type
