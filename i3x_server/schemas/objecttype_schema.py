"""Object-type and DataType JSON Schema builders.

Builds JSON Schema documents from OPC UA ObjectTypes (lineage-merged ``allOf``
with ``$defs``) and from structured DataType definitions. The annotation
machinery lives in ``objecttype_annotations.py``, the definition registry and
``$ref`` post-processing in ``objecttype_registry.py``. For private internals,
tests and monkeypatching must target the source modules.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any, cast

from asyncua import ua
from asyncua.ua.uaprotocol_auto import Argument

from i3x_server.domain.ports.opcua import OpcUaNamespaceInfo, OpcUaObjectTypeInfo, OpcUaObjectTypeMemberInfo
from i3x_server.domain.utils import expanded_node_id
from i3x_server.schemas import objecttype_annotations, objecttype_registry
from i3x_server.schemas.objecttype_registry import (
    deduplicate_schema_fragments,
    remove_opcua_schema_fields,
)

__all__ = [
    "build_data_type_schema",
    "build_object_type_schema",
    "deduplicate_schema_fragments",
    "json_schema_for_opcua_type",
    "remove_opcua_schema_fields",
]

_MANDATORY_RULES = {"mandatory", "mandatoryplaceholder"}


def _ua_object_id_name(identifier: int) -> str | None:
    object_id_names = getattr(ua, "ObjectIdNames", None)
    if isinstance(object_id_names, Mapping):
        candidate = object_id_names.get(identifier)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def json_schema_for_opcua_type(data_type: str | None) -> dict[str, Any]:
    if data_type is None:
        return {}

    type_name_token = data_type
    normalized = data_type.lower()
    node_id_match = re.fullmatch(r"(?:ns=0;|nsu=http://opcfoundation.org/UA/;)?i=(\d+)", data_type, flags=re.IGNORECASE)
    if node_id_match is not None:
        object_id_name = _ua_object_id_name(int(node_id_match.group(1)))
        if object_id_name:
            type_name_token = object_id_name
            normalized = object_id_name.lower()

    elif re.match(r"^(?:ns=|nsu=|[isgb]=)", data_type, flags=re.IGNORECASE):
        # Identifiers in other namespaces are not OPC UA built-in type identifiers.
        return {}

    if normalized == "bytestring":
        return {"type": "string", "contentEncoding": "base64"}
    if normalized in {"string", "guid", "xmlelement"}:
        return {"type": "string"}
    if "boolean" in normalized:
        return {"type": "boolean"}
    if normalized == "number" or any(token in normalized for token in ["double", "float"]):
        return {"type": "number"}
    if normalized in {"integer", "uinteger"} or any(
        token in normalized
        for token in [
            "sbyte",
            "byte",
            "int16",
            "int32",
            "int64",
            "uint16",
            "uint32",
            "uint64",
        ]
    ):
        return {"type": "integer"}
    if "datetime" in normalized:
        return {"type": "string", "format": "date-time"}

    type_candidate = objecttype_annotations._resolve_structure_type_by_name(type_name_token)
    if isinstance(type_candidate, type):
        if type_candidate is bool:
            return {"type": "boolean"}
        if type_candidate.__name__.lower() in {"number"}:
            return {"type": "number"}
        if objecttype_annotations._is_integer_annotation_type(type_candidate):
            return {"type": "integer"}
        if objecttype_annotations._is_number_annotation_type(type_candidate):
            return {"type": "number"}
        if objecttype_annotations._is_datetime_annotation_type(type_candidate):
            return {"type": "string", "format": "date-time"}

    return {}


def build_object_type_schema(
    item: OpcUaObjectTypeInfo,
    object_types_by_node_id: Mapping[str, OpcUaObjectTypeInfo],
    element_ids_by_node_id: Mapping[str, str],
    namespace_infos: list[OpcUaNamespaceInfo],
    *,
    include_opcua_fields: bool = True,
    resolved_data_type_schemas: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    lineage = _lineage(item, object_types_by_node_id)
    registry = objecttype_registry._SchemaRegistry()

    all_of: list[dict[str, str]] = []
    merged_properties: dict[str, Any] = {}
    merged_required: list[str] = []
    required_seen: set[str] = set()

    for ancestor in lineage:
        definition_key = objecttype_registry._def_key(
            objecttype_registry._definition_name(ancestor, element_ids_by_node_id)
        )
        ancestor_schema, ancestor_required = _schema_for_single_type(
            ancestor,
            namespace_infos,
            registry,
            resolved_data_type_schemas,
        )
        registry.set_definition(definition_key, ancestor_schema)
        all_of.append({"$ref": f"#/$defs/{definition_key}"})

        for name, prop_schema in ancestor_schema["properties"].items():
            merged_properties[name] = prop_schema

        for name in ancestor_required:
            if name not in required_seen:
                required_seen.add(name)
                merged_required.append(name)

    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "title": item.display_name,
        "x-opcua-nodeId": expanded_node_id(item.node_id, namespace_infos),
        "x-opcua-displayName": item.display_name,
        "properties": merged_properties,
        "$defs": registry.defs,
        "allOf": all_of,
    }
    if item.parent_node_id:
        schema["x-opcua-superTypeNodeId"] = expanded_node_id(item.parent_node_id, namespace_infos)
        schema["x-opcua-typeDefinition"] = expanded_node_id(item.parent_node_id, namespace_infos)
    type_references = _references_for_type(item, namespace_infos)
    if type_references:
        schema["x-opcua-references"] = type_references
    description = getattr(item, "description", None)
    if description:
        schema["description"] = description
    is_abstract = getattr(item, "is_abstract", None)
    if is_abstract is not None:
        schema["x-opcua-isAbstract"] = is_abstract
    if merged_required:
        schema["required"] = merged_required
    objecttype_registry._dedupe_defs_and_rewrite_local_refs(schema)
    if not include_opcua_fields:
        return cast(dict[str, Any], remove_opcua_schema_fields(schema))
    return schema


def _lineage(
    item: OpcUaObjectTypeInfo,
    object_types_by_node_id: Mapping[str, OpcUaObjectTypeInfo],
) -> list[OpcUaObjectTypeInfo]:
    chain: list[OpcUaObjectTypeInfo] = []
    seen: set[str] = set()

    current: OpcUaObjectTypeInfo | None = item
    while current is not None and current.node_id not in seen:
        chain.append(current)
        seen.add(current.node_id)
        parent_id = current.parent_node_id
        if not parent_id:
            break
        current = object_types_by_node_id.get(parent_id)

    chain.reverse()
    return chain


def _schema_for_single_type(
    item: OpcUaObjectTypeInfo,
    namespace_infos: list[OpcUaNamespaceInfo],
    registry: objecttype_registry._SchemaRegistry,
    resolved_data_type_schemas: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    required_seen: set[str] = set()

    for member in _members(item):
        prop_schema = _schema_for_member(member, namespace_infos, registry, resolved_data_type_schemas)
        properties[member.browse_name] = prop_schema
        normalized_rule = (member.modelling_rule or "").strip().lower()
        if normalized_rule in _MANDATORY_RULES and member.browse_name not in required_seen:
            required_seen.add(member.browse_name)
            required.append(member.browse_name)

    output: dict[str, Any] = {
        "type": "object",
        "title": item.display_name,
        "x-opcua-nodeId": expanded_node_id(item.node_id, namespace_infos),
        "x-opcua-displayName": item.display_name,
        "properties": properties,
    }
    if item.parent_node_id:
        output["x-opcua-superTypeNodeId"] = expanded_node_id(item.parent_node_id, namespace_infos)
    description = getattr(item, "description", None)
    if description:
        output["description"] = description
    is_abstract = getattr(item, "is_abstract", None)
    if is_abstract is not None:
        output["x-opcua-isAbstract"] = is_abstract
    if required:
        output["required"] = required

    type_references = _references_for_type(item, namespace_infos)
    if type_references:
        output["x-opcua-references"] = type_references

    return output, required


def _schema_for_member(
    member: OpcUaObjectTypeMemberInfo,
    namespace_infos: list[OpcUaNamespaceInfo],
    registry: objecttype_registry._SchemaRegistry,
    resolved_data_type_schemas: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    data_type_id = getattr(member, "data_type", None)
    normalized_data_type_id = (
        _expanded_if_node_id(_node_id_to_string(data_type_id), namespace_infos) if data_type_id else None
    )
    resolved_schema = (
        resolved_data_type_schemas.get(normalized_data_type_id.lower())
        if normalized_data_type_id and resolved_data_type_schemas
        else None
    )
    schema_source_value = getattr(member, "schema_value", member.value)
    structured_schema = _schema_for_structured_value(
        schema_source_value,
        member.data_type,
        getattr(member, "variant_type", None),
        _member_is_array(member),
        namespace_infos,
        registry,
    )
    if member.node_class.lower() == "object":
        # An Object member is structured, but that must not discard its resolved DataType schema or
        # its ValueRank shape: an object member with ValueRank -2/-3/>=1 carries array values.
        schema = dict(resolved_schema) if resolved_schema is not None else (structured_schema or {"type": "object"})
        if _member_is_array(member):
            schema = {"type": "array", "items": schema}
    elif resolved_schema is not None:
        schema = dict(resolved_schema)
        if _member_is_array(member):
            schema = {"type": "array", "items": schema}
    elif structured_schema is not None:
        schema = structured_schema
    else:
        schema = json_schema_for_opcua_type(member.data_type)

    # Keep "$ref" pure by moving member-level metadata beside an allOf wrapper.
    if "$ref" in schema:
        schema = {"allOf": [schema]}

    if member.modelling_rule:
        schema["x-opcua-modellingRule"] = member.modelling_rule
    member_data_type = getattr(member, "data_type", None)
    if member_data_type:
        normalized_data_type = _expanded_if_node_id(_node_id_to_string(member_data_type), namespace_infos)
        schema["x-opcua-dataTypeId"] = normalized_data_type or member_data_type
    member_value_rank = getattr(member, "value_rank", None)
    if isinstance(member_value_rank, int):
        schema["x-opcua-valueRank"] = member_value_rank
    member_array_dimensions = getattr(member, "array_dimensions", None)
    if isinstance(member_array_dimensions, list) and member_array_dimensions:
        schema["x-opcua-arrayDimensions"] = [int(value) for value in member_array_dimensions]
    member_type_definition_id = getattr(member, "type_definition_id", None)
    if isinstance(member_type_definition_id, str) and member_type_definition_id:
        normalized_type_definition_id = _expanded_if_node_id(
            _node_id_to_string(member_type_definition_id),
            namespace_infos,
        )
        schema["x-opcua-typeDefinition"] = normalized_type_definition_id or member_type_definition_id
    schema["x-opcua-nodeId"] = expanded_node_id(member.node_id, namespace_infos)
    schema["x-opcua-displayName"] = member.display_name
    member_reference_type_id = getattr(member, "reference_type_id", None)
    member_reference_type = getattr(member, "reference_type", None)
    if isinstance(member_reference_type_id, str) and member_reference_type_id:
        normalized_reference_type_id = _expanded_if_node_id(
            _node_id_to_string(member_reference_type_id),
            namespace_infos,
        )
        schema["x-opcua-referenceTypeId"] = normalized_reference_type_id or member_reference_type_id
    if isinstance(member_reference_type, str) and member_reference_type:
        schema["x-opcua-referenceType"] = member_reference_type
    member_reference_order = getattr(member, "reference_order", None)
    if isinstance(member_reference_order, int):
        schema["x-opcua-referenceOrder"] = member_reference_order
    member_references = _references_for_member(member, namespace_infos)
    if member_references:
        schema["x-opcua-references"] = member_references
    if member.description:
        schema["description"] = member.description
    if member.value is not None:
        schema["x-opcua-value"] = _to_json_metadata_value(member.value, namespace_infos)
    if member.display_name and member.display_name != member.browse_name:
        schema["title"] = member.display_name
    return schema


def _schema_for_structured_value(
    value: Any,
    data_type: str | None,
    variant_type: str | None,
    is_array: bool | None,
    namespace_infos: list[OpcUaNamespaceInfo],
    registry: objecttype_registry._SchemaRegistry,
) -> dict[str, Any] | None:
    variant = _extract_variant(value)
    unwrapped = _unwrap_opcua_wrapper(value)

    if variant is not None:
        variant_value = getattr(variant, "Value", None)
        if variant_value is not None:
            unwrapped = variant_value

    if isinstance(unwrapped, (list, tuple)) or bool(is_array):
        items_schema = _schema_for_array_items(unwrapped, data_type, variant_type, namespace_infos, registry)
        if items_schema is None:
            return None
        return {"type": "array", "items": items_schema}

    if _is_extension_object(unwrapped):
        body = _unwrap_opcua_wrapper(getattr(unwrapped, "Body", None))
        raw_type_id = _node_id_to_string(getattr(unwrapped, "TypeId", None))
        type_id = _expanded_if_node_id(raw_type_id, namespace_infos)
        metadata: dict[str, Any] = {}
        if type_id and not _is_null_node_id(type_id):
            metadata["x-opcua-structureTypeId"] = type_id
        if body is not None:
            schema_key = f"opcua-structure:{type_id or type(body).__name__.lower()}"
            return _reference_or_register_structure(schema_key, body, registry, metadata)

        type_schema = _schema_from_data_type(data_type or type_id, namespace_infos, registry)
        if type_schema is not None:
            if metadata:
                type_schema = {**type_schema, **metadata}
            return type_schema
        return {"type": "object", **metadata} if metadata else {"type": "object"}

    if _is_structure_value(unwrapped):
        schema_key = _structure_schema_key(unwrapped)
        return _reference_or_register_structure(schema_key, unwrapped, registry, {})

    if variant_type and "extensionobject" in variant_type.lower():
        type_schema = _schema_from_data_type(data_type, namespace_infos, registry)
        if type_schema is not None:
            return type_schema
        return {"type": "object"}

    return None


def _schema_for_array_items(
    values: Any,
    data_type: str | None,
    variant_type: str | None,
    namespace_infos: list[OpcUaNamespaceInfo],
    registry: objecttype_registry._SchemaRegistry,
) -> dict[str, Any] | None:
    if isinstance(values, (list, tuple)) and values:
        inferred = _schema_for_structured_value(values[0], data_type, variant_type, False, namespace_infos, registry)
        if inferred is not None:
            return inferred

        if _is_extension_variant(variant_type):
            from_data_type = _schema_from_data_type(data_type, namespace_infos, registry)
            if from_data_type is not None:
                return from_data_type
            return {"type": "object"}

        return _primitive_schema_for_value(values[0])

    if _is_extension_variant(variant_type):
        from_data_type = _schema_from_data_type(data_type, namespace_infos, registry)
        if from_data_type is not None:
            return from_data_type
        return {"type": "object"}

    return json_schema_for_opcua_type(data_type)


def _reference_or_register_structure(
    schema_key: str,
    structure_value: Any,
    registry: objecttype_registry._SchemaRegistry,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    canonical_key = objecttype_annotations._canonical_schema_key_for_structure_value(structure_value)

    if registry.has(schema_key):
        ref = registry.ref_for(schema_key)
    elif canonical_key and registry.has(canonical_key):
        registry.reserve(schema_key, registry.definition_name_for(canonical_key))
        ref = registry.ref_for(schema_key)
    else:
        preferred_name = type(structure_value).__name__
        if isinstance(structure_value, Mapping):
            preferred_name = "MappedStructure"
        definition_name = registry.allocate(schema_key, preferred_name)
        if canonical_key and not registry.has(canonical_key):
            registry.reserve(canonical_key, definition_name)
        structure_type = type(structure_value)
        if (
            canonical_key
            and not isinstance(structure_value, Mapping)
            and objecttype_annotations._is_structured_class(structure_type)
        ):
            structure_schema = objecttype_annotations._structure_schema_for_type(structure_type, registry)
        else:
            structure_schema = _structure_object_schema(structure_value, registry)
        for key, value in metadata.items():
            structure_schema[key] = value
        registry.set_definition(definition_name, structure_schema)
        ref = registry.ref_for(schema_key)
    return ref


def _structure_object_schema(value: Any, registry: objecttype_registry._SchemaRegistry) -> dict[str, Any]:
    if isinstance(value, Argument):
        return objecttype_annotations._structure_schema_for_type(type(value), registry)
    fields = _structure_fields(value)
    properties: dict[str, Any] = {}

    for key, field_value in fields.items():
        nested_schema = _schema_for_structured_value(field_value, None, None, None, [], registry)
        if nested_schema is not None:
            properties[key] = nested_schema
            continue
        properties[key] = _primitive_schema_for_value(field_value)

    schema: dict[str, Any] = {
        "type": "object",
        "title": type(value).__name__,
        "properties": properties,
        "additionalProperties": False,
    }
    return schema


def _structure_fields(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {
            str(key): item
            for key, item in value.items()
            if not objecttype_annotations._is_filtered_structure_field(str(key))
        }
    if is_dataclass(value):
        return {
            item.name: getattr(value, item.name)
            for item in fields(value)
            if not objecttype_annotations._is_filtered_structure_field(item.name)
        }
    if hasattr(value, "__dict__"):
        return {
            str(key): item
            for key, item in vars(value).items()
            if not key.startswith("_")
            and not callable(item)
            and not objecttype_annotations._is_filtered_structure_field(str(key))
        }
    return {}


def _primitive_schema_for_value(value: Any) -> dict[str, Any]:
    value = _unwrap_opcua_wrapper(value)
    if value is None:
        return {"type": ["null", "string"]}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"type": "string", "contentEncoding": "base64"}
    if isinstance(value, (list, tuple)):
        if not value:
            return {"type": "array", "items": {"type": "string"}}
        item_schema = _primitive_schema_for_value(value[0])
        return {"type": "array", "items": item_schema}
    if isinstance(value, Mapping) or _is_structure_value(value):
        return {"type": "object"}
    return {"type": "string"}


def _unwrap_opcua_wrapper(value: Any) -> Any:
    current = value
    for _ in range(3):
        class_name = type(current).__name__ if current is not None else ""
        if class_name == "DataValue":
            current = getattr(current, "Value", None)
            continue
        if class_name == "Variant":
            current = getattr(current, "Value", None)
            continue
        break
    return current


def _extract_variant(value: Any) -> Any | None:
    if value is None:
        return None
    if type(value).__name__ == "Variant":
        return value
    if type(value).__name__ == "DataValue":
        return getattr(value, "Value", None)
    return None


def _is_extension_object(value: Any) -> bool:
    if value is None:
        return False
    class_name = type(value).__name__
    if class_name == "ExtensionObject":
        return True
    return hasattr(value, "Body") and hasattr(value, "TypeId")


def _is_extension_variant(variant_type: str | None) -> bool:
    if variant_type is None:
        return False
    return "extensionobject" in variant_type.lower()


def _member_is_array(member: OpcUaObjectTypeMemberInfo) -> bool | None:
    """Whether the member must be modelled as an array.

    ``ValueRank >= 1`` is a fixed-dimension array. ``-2`` (OneOrMoreDimensions) and ``-3``
    (ScalarOrOneDimension) explicitly allow array values, so modelling them as scalars would reject
    legitimate arrays (OPC UA Part 8, ValueRank enumeration).
    """
    if getattr(member, "is_array", None) is True:
        return True
    value_rank = getattr(member, "value_rank", None)
    if isinstance(value_rank, int) and (value_rank >= 1 or value_rank in (-2, -3)):
        return True
    return getattr(member, "is_array", None)


def _is_structure_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, Mapping):
        return True
    if isinstance(value, Enum):
        return False
    if is_dataclass(value):
        return True
    if hasattr(value, "__dict__"):
        return type(value).__module__ != "builtins"
    return False


def _structure_schema_key(value: Any) -> str:
    if isinstance(value, Mapping):
        key_signature = ",".join(sorted(str(key) for key in value.keys()))
        return f"map-structure:{key_signature or 'empty'}"
    module = type(value).__module__.lower()
    name = type(value).__name__.lower()
    return f"py-structure:{module}.{name}"


def _schema_from_data_type(
    data_type: str | None,
    namespace_infos: list[OpcUaNamespaceInfo],
    registry: objecttype_registry._SchemaRegistry,
) -> dict[str, Any] | None:
    if not data_type:
        return None

    normalized_data_type = _expanded_if_node_id(_node_id_to_string(data_type), namespace_infos) or data_type
    structure_type = _resolve_structure_type(normalized_data_type)
    if structure_type is None:
        indexed_node_id = _indexed_node_id(normalized_data_type, namespace_infos)
        if indexed_node_id is not None:
            structure_type = _resolve_structure_type(indexed_node_id)
    if structure_type is None:
        return None
    if not objecttype_annotations._is_structured_class(structure_type):
        return None

    schema_key = f"opcua-datatype:{normalized_data_type.lower()}"
    metadata = {"x-opcua-structureDataType": normalized_data_type}
    return objecttype_annotations._reference_or_register_structure_from_type(
        schema_key, structure_type, registry, metadata
    )


def build_data_type_schema(
    data_type: str,
    namespace_infos: list[OpcUaNamespaceInfo],
    *,
    include_opcua_fields: bool = True,
) -> dict[str, Any] | None:
    registry = objecttype_registry._SchemaRegistry()
    schema = _schema_from_data_type(data_type, namespace_infos, registry)
    if schema is None:
        return None

    inlined = objecttype_registry._inline_registered_reference(schema, registry)
    expanded = objecttype_registry._expand_schema_refs(inlined, registry.defs)
    if isinstance(expanded, Mapping):
        schema_dict = dict(expanded)
        if not include_opcua_fields:
            return cast(dict[str, Any], remove_opcua_schema_fields(schema_dict))
        return schema_dict
    return None


def _resolve_structure_type(data_type: str) -> type[Any] | None:
    canonical = _node_id_to_string(data_type) or str(data_type)

    symbolic_name: str | None = None
    id_match = re.match(r"^(?:ns=0;|nsu=http://opcfoundation.org/UA/;)i=(\d+)$", canonical, flags=re.IGNORECASE)
    if id_match is not None:
        symbolic_name = _ua_object_id_name(int(id_match.group(1)))

    name_candidate = objecttype_annotations._resolve_structure_type_by_name(symbolic_name or canonical)
    if isinstance(name_candidate, type):
        return name_candidate

    registries = [
        getattr(ua, "extension_objects_by_datatype", None),
        getattr(ua, "extension_objects_by_typeid", None),
        getattr(ua, "EXTENSION_OBJECT_CLASSES_BY_DATATYPE", None),
    ]
    for registry in registries:
        if not isinstance(registry, Mapping):
            continue
        for key, value in registry.items():
            key_text = _node_id_to_string(key) or str(key)
            if key_text == canonical:
                return value if isinstance(value, type) else None
    return None


def _node_id_to_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    to_string = getattr(value, "to_string", None)
    if callable(to_string):
        try:
            return str(to_string())
        except Exception:
            return str(value)
    return str(value)


def _to_json_metadata_value(value: Any, namespace_infos: list[OpcUaNamespaceInfo]) -> Any:
    current = _unwrap_opcua_wrapper(value)
    if current is None or isinstance(current, (str, int, float, bool)):
        return current
    if isinstance(current, datetime):
        return current.isoformat()
    if isinstance(current, (bytes, bytearray, memoryview)):
        return {"encoding": "base64", "length": len(current)}
    if isinstance(current, (list, tuple)):
        return [_to_json_metadata_value(item, namespace_infos) for item in current]
    if isinstance(current, Mapping):
        return {str(key): _to_json_metadata_value(item, namespace_infos) for key, item in current.items()}
    if is_dataclass(current):
        return {
            item.name: _to_json_metadata_value(getattr(current, item.name), namespace_infos) for item in fields(current)
        }
    if _is_extension_object(current):
        raw_type_id = _node_id_to_string(getattr(current, "TypeId", None))
        type_id = _expanded_if_node_id(raw_type_id, namespace_infos)
        body = _to_json_metadata_value(getattr(current, "Body", None), namespace_infos)
        return {"TypeId": type_id, "Body": body}
    if hasattr(current, "__dict__") and type(current).__module__ != "builtins":
        return {
            str(key): _to_json_metadata_value(item, namespace_infos)
            for key, item in vars(current).items()
            if not key.startswith("_") and not callable(item)
        }
    expanded_candidate = _expanded_if_node_id(_node_id_to_string(current), namespace_infos)
    if expanded_candidate is not None:
        return expanded_candidate
    return str(current)


def _expanded_if_node_id(value: str | None, namespace_infos: list[OpcUaNamespaceInfo]) -> str | None:
    if value is None:
        return None
    return expanded_node_id(value, namespace_infos)


def _indexed_node_id(value: str, namespace_infos: list[OpcUaNamespaceInfo]) -> str | None:
    match = re.match(r"^nsu=([^;]+);([isgb]=.+)$", value)
    if match is None:
        return None

    namespace_uri = match.group(1)
    identifier = match.group(2)

    # Namespace 0 (OPC UA standard namespace) is fixed and does not depend
    # on the runtime namespace array ordering.
    if namespace_uri.rstrip("/").lower() == "http://opcfoundation.org/ua":
        return f"ns=0;{identifier}"

    for index, namespace_info in enumerate(namespace_infos):
        if namespace_info.uri == namespace_uri:
            return f"ns={index};{identifier}"
    return None


def _is_null_node_id(value: str) -> bool:
    normalized = value.strip().lower()
    return bool(re.match(r"^(?:ns=\d+;|nsu=[^;]+;)?i=0$", normalized))


def _members(item: OpcUaObjectTypeInfo) -> Iterable[OpcUaObjectTypeMemberInfo]:
    members = getattr(item, "members", None)
    if isinstance(members, list):
        return members

    properties = getattr(item, "properties", None)
    if isinstance(properties, Mapping):
        fallback: list[OpcUaObjectTypeMemberInfo] = []
        for name, data_type in properties.items():
            fallback.append(
                OpcUaObjectTypeMemberInfo(
                    node_id=f"{item.node_id}:{name}",
                    browse_name=name,
                    display_name=name,
                    description=None,
                    node_class="Variable",
                    data_type=data_type,
                    value=None,
                    modelling_rule=None,
                )
            )
        return fallback

    return []


def _references_for_type(item: OpcUaObjectTypeInfo, namespace_infos: list[OpcUaNamespaceInfo]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    if item.parent_node_id:
        refs.append(
            {
                "referenceType": "HasSubtype",
                "targetNodeId": expanded_node_id(item.parent_node_id, namespace_infos),
            }
        )
    return refs


def _references_for_member(
    member: OpcUaObjectTypeMemberInfo,
    namespace_infos: list[OpcUaNamespaceInfo],
) -> list[dict[str, Any]]:
    parent_node_id = getattr(member, "parent_node_id", None)
    if not isinstance(parent_node_id, str) or not parent_node_id:
        return []

    reference: dict[str, Any] = {
        "sourceNodeId": expanded_node_id(parent_node_id, namespace_infos),
        "targetNodeId": expanded_node_id(member.node_id, namespace_infos),
    }

    reference_type = getattr(member, "reference_type", None)
    if isinstance(reference_type, str) and reference_type:
        reference["referenceType"] = reference_type

    reference_type_id = getattr(member, "reference_type_id", None)
    if isinstance(reference_type_id, str) and reference_type_id:
        normalized_reference_type_id = _expanded_if_node_id(_node_id_to_string(reference_type_id), namespace_infos)
        reference["referenceTypeId"] = normalized_reference_type_id or reference_type_id

    reference_order = getattr(member, "reference_order", None)
    if isinstance(reference_order, int):
        reference["order"] = reference_order

    return [reference]
