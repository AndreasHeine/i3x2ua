from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from contextlib import nullcontext as _nullcontext
from copy import deepcopy
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from asyncua import ua

from i3x_server.api.v1.contracts import ObjectTypeResponse
from i3x_server.api.v1.object_helpers import (
    _canonical_namespace_uri,
    _expanded_node_id,
    _is_null_opcua_type_node_id,
    _namespace_uri_for_node_id,
    _namespace_uri_from_expanded_node_id,
    _object_type_element_ids_by_node_id,
    _to_object_type,
    _unknown_type_element_id,
    _virtual_object_type_element_id,
)
from i3x_server.application.ports.opcua import OpcUaClientProtocol, OpcUaNamespaceInfo, OpcUaObjectTypeInfo
from i3x_server.config.settings import Settings, get_settings
from i3x_server.errors import i3x_http_error
from i3x_server.schemas.objecttype_schema import (
    build_data_type_schema,
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
                elementId=_virtual_object_type_element_id(namespace_uri, display_name, source_type_id),
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
    if inferred == {"type": "string"}:
        return None
    return inferred


def _datatype_object_type_from_source_type_id(
    source_type_id: str,
    namespace_infos: list[OpcUaNamespaceInfo],
) -> ObjectTypeResponse | None:
    schema = build_data_type_schema(source_type_id, namespace_infos)
    if not isinstance(schema, Mapping):
        scalar_schema: dict[str, Any] | None = None
        if _is_builtin_ua_datatype_node_id(source_type_id):
            scalar_schema = json_schema_for_opcua_type(source_type_id)
        else:
            scalar_schema = _scalar_schema_for_standard_ua_datatype_node_id(source_type_id)
        if scalar_schema is None:
            return None
        schema = {
            "oneOf": [
                {"type": "null"},
                dict(scalar_schema),
                {"type": "array", "items": _array_items_schema_for_builtin_data_type(source_type_id, scalar_schema)},
            ]
        }

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
        elementId=_virtual_object_type_element_id(namespace_uri, display_name, source_type_id),
        displayName=display_name,
        namespaceUri=namespace_uri,
        sourceTypeId=source_type_id,
        schema=schema_payload,
    )


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
        elementId=_virtual_object_type_element_id(namespace_uri, display_name, source_type_id),
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
        elementId=_virtual_object_type_element_id(canonical_namespace_uri, display_name, source_type_id),
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

    items: list[ObjectTypeResponse] = []
    chunk_size = 50
    for i in range(0, len(object_types), chunk_size):
        chunk = object_types[i : i + chunk_size]
        items.extend(
            [
                _to_object_type(item, model, namespace_infos, object_types_by_node_id, element_ids_by_node_id, {})
                for item in chunk
            ]
        )
        await asyncio.sleep(0)

    items.extend(_synthetic_object_types_from_structure_defs(items, namespace_infos))

    referenced_type_element_ids = _collect_referenced_type_element_ids(model, namespace_infos, element_ids_by_node_id)
    property_type_element_ids = _collect_property_type_element_ids(model, namespace_infos)
    known_ids = {item.elementId for item in items}
    by_source_type_id: dict[str, ObjectTypeResponse] = {item.sourceTypeId.lower(): item for item in items}
    browse_name_cache: dict[str, str | None] = {}
    lookup_budget = {"remaining": _live_type_name_lookup_max_per_request()}

    for unresolved_id in sorted(referenced_type_element_ids - known_ids):
        unresolved_key = unresolved_id.lower()
        source_match = by_source_type_id.get(unresolved_key)
        if source_match is not None:
            items.append(_object_type_alias_with_element_id(source_match, unresolved_id))
            continue

        datatype_item = _datatype_object_type_from_source_type_id(unresolved_id, namespace_infos)
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


async def _get_object_type_context(
    request: Any,
    model: BuildResult,
    opcua_client: OpcUaClientProtocol,
    namespace_infos: list[OpcUaNamespaceInfo] | None = None,
) -> _ObjectTypeContext:
    started = perf_counter()
    resolved_namespace_infos = (
        namespace_infos if namespace_infos is not None else await opcua_client.get_namespace_infos()
    )
    object_types = await opcua_client.get_object_types()

    lock = getattr(request.app.state, "object_type_lock", None)
    async with lock if lock else _nullcontext():
        cache = getattr(request.app.state, "object_type_context_cache", None)
        model_token = id(model)
        namespace_token = id(resolved_namespace_infos)
        object_types_token = id(object_types)
        if isinstance(cache, dict):
            if (
                cache.get("model_token") == model_token
                and cache.get("namespace_token") == namespace_token
                and cache.get("object_types_token") == object_types_token
            ):
                cached_context = cache.get("context")
                if isinstance(cached_context, _ObjectTypeContext):
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

    return namespace_infos, object_type_element_ids_by_node_id, object_type_element_ids_by_source_type
