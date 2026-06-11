from __future__ import annotations

import importlib
import re
import sys
import typing
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import MISSING, fields, is_dataclass
from datetime import datetime
from enum import Enum
from types import ModuleType
from typing import Any, get_args, get_origin

from asyncua import ua

from i3x_server.opcua.client import OpcUaNamespaceInfo, OpcUaObjectTypeInfo, OpcUaObjectTypeMemberInfo

_MANDATORY_RULES = {"mandatory", "mandatoryplaceholder"}


def _ua_object_id_name(identifier: int) -> str | None:
    object_id_names = getattr(ua, "ObjectIdNames", None)
    if isinstance(object_id_names, Mapping):
        candidate = object_id_names.get(identifier)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


class _SchemaRegistry:
    def __init__(self) -> None:
        self.defs: dict[str, Any] = {}
        self._definition_by_key: dict[str, str] = {}
        self._used_definition_names: set[str] = set()

    def has(self, key: str) -> bool:
        return key in self._definition_by_key

    def ref_for(self, key: str) -> dict[str, str]:
        definition_name = self._definition_by_key[key]
        return {"$ref": f"#/$defs/{definition_name}"}

    def definition_name_for(self, key: str) -> str:
        return self._definition_by_key[key]

    def reserve(self, key: str, definition_name: str) -> None:
        self._definition_by_key[key] = definition_name
        self._used_definition_names.add(definition_name)

    def set_definition(self, definition_name: str, schema: dict[str, Any]) -> None:
        self.defs[definition_name] = schema

    def allocate(self, key: str, preferred_name: str) -> str:
        if key in self._definition_by_key:
            return self._definition_by_key[key]

        base_name = _safe_definition_name(preferred_name)
        candidate = base_name
        index = 2
        while candidate in self._used_definition_names:
            candidate = f"{base_name}_{index}"
            index += 1

        self.reserve(key, candidate)
        return candidate


def json_schema_for_opcua_type(data_type: str | None) -> dict[str, Any]:
    if data_type is None:
        return {"type": "string"}

    type_name_token = data_type
    normalized = data_type.lower()
    node_id_match = re.match(r"^(?:ns=0;|nsu=http://opcfoundation.org/UA/;)i=(\d+)$", data_type, flags=re.IGNORECASE)
    if node_id_match is not None:
        object_id_name = _ua_object_id_name(int(node_id_match.group(1)))
        if object_id_name:
            type_name_token = object_id_name
            normalized = object_id_name.lower()

    if normalized.endswith("i=10") or normalized.endswith("i=11"):
        return {"type": "number"}
    if any(normalized.endswith(f"i={idx}") for idx in [2, 3, 4, 5, 6, 7, 8, 9]):
        return {"type": "integer"}
    # DateTime (i=13) and subtypes
    if normalized.endswith("i=13"):
        return {"type": "string", "format": "date-time"}

    if "boolean" in normalized or normalized.endswith("i=1"):
        return {"type": "boolean"}
    if any(token in normalized for token in ["double", "float"]):
        return {"type": "number"}
    if any(
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

    type_candidate = _resolve_structure_type_by_name(type_name_token)
    if isinstance(type_candidate, type):
        if type_candidate is bool:
            return {"type": "boolean"}
        if type_candidate.__name__.lower() in {"number"}:
            return {"type": "number"}
        if _is_integer_annotation_type(type_candidate):
            return {"type": "integer"}
        if _is_number_annotation_type(type_candidate):
            return {"type": "number"}
        if _is_datetime_annotation_type(type_candidate):
            return {"type": "string", "format": "date-time"}

    return {"type": "string"}


def build_object_type_schema(
    item: OpcUaObjectTypeInfo,
    object_types_by_node_id: Mapping[str, OpcUaObjectTypeInfo],
    element_ids_by_node_id: Mapping[str, str],
    namespace_infos: list[OpcUaNamespaceInfo],
) -> dict[str, Any]:
    lineage = _lineage(item, object_types_by_node_id)
    registry = _SchemaRegistry()

    all_of: list[dict[str, str]] = []
    merged_properties: dict[str, Any] = {}
    merged_required: list[str] = []
    required_seen: set[str] = set()

    for ancestor in lineage:
        definition_key = _def_key(_definition_name(ancestor, element_ids_by_node_id))
        ancestor_schema, ancestor_required = _schema_for_single_type(ancestor, namespace_infos, registry)
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
        "x-opcua-nodeId": _expanded_node_id(item.node_id, namespace_infos),
        "x-opcua-displayName": item.display_name,
        "properties": merged_properties,
        "$defs": registry.defs,
        "allOf": all_of,
    }
    description = getattr(item, "description", None)
    if description:
        schema["description"] = description
        schema["x-opcua-description"] = description
    is_abstract = getattr(item, "is_abstract", None)
    if is_abstract is not None:
        schema["x-opcua-isAbstract"] = is_abstract
    if merged_required:
        schema["required"] = merged_required
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
    registry: _SchemaRegistry,
) -> tuple[dict[str, Any], list[str]]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    required_seen: set[str] = set()

    for member in _members(item):
        prop_schema = _schema_for_member(member, namespace_infos, registry)
        properties[member.browse_name] = prop_schema
        normalized_rule = (member.modelling_rule or "").strip().lower()
        if normalized_rule in _MANDATORY_RULES and member.browse_name not in required_seen:
            required_seen.add(member.browse_name)
            required.append(member.browse_name)

    output: dict[str, Any] = {
        "type": "object",
        "title": item.display_name,
        "x-opcua-nodeId": _expanded_node_id(item.node_id, namespace_infos),
        "x-opcua-displayName": item.display_name,
        "properties": properties,
    }
    description = getattr(item, "description", None)
    if description:
        output["description"] = description
        output["x-opcua-description"] = description
    is_abstract = getattr(item, "is_abstract", None)
    if is_abstract is not None:
        output["x-opcua-isAbstract"] = is_abstract
    if required:
        output["required"] = required

    return output, required


def _schema_for_member(
    member: OpcUaObjectTypeMemberInfo,
    namespace_infos: list[OpcUaNamespaceInfo],
    registry: _SchemaRegistry,
) -> dict[str, Any]:
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
        schema: dict[str, Any] = {"type": "object"}
    elif structured_schema is not None:
        schema = structured_schema
    else:
        schema = json_schema_for_opcua_type(member.data_type)

    # Keep "$ref" pure by moving member-level metadata beside an allOf wrapper.
    if "$ref" in schema:
        schema = {"allOf": [schema]}

    if member.modelling_rule:
        schema["x-opcua-modellingRule"] = member.modelling_rule
    schema["x-opcua-nodeId"] = _expanded_node_id(member.node_id, namespace_infos)
    schema["x-opcua-displayName"] = member.display_name
    if member.description:
        schema["description"] = member.description
        schema["x-opcua-description"] = member.description
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
    registry: _SchemaRegistry,
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
    registry: _SchemaRegistry,
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

    if data_type is not None:
        return json_schema_for_opcua_type(data_type)

    return {"type": "string"}


def _reference_or_register_structure(
    schema_key: str,
    structure_value: Any,
    registry: _SchemaRegistry,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    canonical_key = _canonical_schema_key_for_structure_value(structure_value)

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
        structure_schema = _structure_object_schema(structure_value, registry)
        for key, value in metadata.items():
            structure_schema[key] = value
        registry.set_definition(definition_name, structure_schema)
        ref = registry.ref_for(schema_key)
    return ref


def _structure_object_schema(value: Any, registry: _SchemaRegistry) -> dict[str, Any]:
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
        return {str(key): item for key, item in value.items()}
    if is_dataclass(value):
        return {item.name: getattr(value, item.name) for item in fields(value)}
    if hasattr(value, "__dict__"):
        return {str(key): item for key, item in vars(value).items() if not key.startswith("_") and not callable(item)}
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
    if getattr(member, "is_array", None) is True:
        return True
    value_rank = getattr(member, "value_rank", None)
    if isinstance(value_rank, int) and value_rank >= 1:
        return True
    return getattr(member, "is_array", None)


def _is_structure_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, Mapping):
        return True
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
    registry: _SchemaRegistry,
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
    if not _is_structured_class(structure_type):
        return None

    schema_key = f"opcua-datatype:{normalized_data_type.lower()}"
    metadata = {"x-opcua-structureDataType": normalized_data_type}
    return _reference_or_register_structure_from_type(schema_key, structure_type, registry, metadata)


def build_data_type_schema(
    data_type: str,
    namespace_infos: list[OpcUaNamespaceInfo],
) -> dict[str, Any] | None:
    registry = _SchemaRegistry()
    schema = _schema_from_data_type(data_type, namespace_infos, registry)
    if schema is None:
        return None

    inlined = _inline_registered_reference(schema, registry)
    expanded = _expand_schema_refs(inlined, registry.defs)
    if isinstance(expanded, Mapping):
        return dict(expanded)
    return None


def _resolve_structure_type(data_type: str) -> type[Any] | None:
    canonical = _node_id_to_string(data_type) or str(data_type)

    symbolic_name: str | None = None
    id_match = re.match(r"^(?:ns=0;|nsu=http://opcfoundation.org/UA/;)i=(\d+)$", canonical, flags=re.IGNORECASE)
    if id_match is not None:
        symbolic_name = _ua_object_id_name(int(id_match.group(1)))

    name_candidate = _resolve_structure_type_by_name(symbolic_name or canonical)
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


def _reference_or_register_structure_from_type(
    schema_key: str,
    structure_type: type[Any],
    registry: _SchemaRegistry,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    canonical_key = _canonical_schema_key_for_type(structure_type)

    if registry.has(schema_key):
        _merge_metadata_into_registered_definition(registry, schema_key, metadata)
        ref = registry.ref_for(schema_key)
    elif canonical_key and registry.has(canonical_key):
        registry.reserve(schema_key, registry.definition_name_for(canonical_key))
        _merge_metadata_into_registered_definition(registry, schema_key, metadata)
        ref = registry.ref_for(schema_key)
    else:
        definition_name = registry.allocate(schema_key, structure_type.__name__)
        if canonical_key and not registry.has(canonical_key):
            registry.reserve(canonical_key, definition_name)
        structure_schema = _structure_schema_for_type(structure_type, registry)
        for key, value in metadata.items():
            structure_schema[key] = value
        registry.set_definition(definition_name, structure_schema)
        ref = registry.ref_for(schema_key)
    return ref


def _canonical_schema_key_for_type(structure_type: type[Any]) -> str | None:
    type_name = structure_type.__name__
    core_name = _structure_core_name(type_name)
    if core_name == "localizedtext":
        return "canonical-structure:localizedtext"

    if type_name.startswith("ISA95") and type_name.endswith("DataType"):
        return f"canonical-structure:isa95-datatype:{core_name}"

    return None


def _canonical_schema_key_for_structure_value(structure_value: Any) -> str | None:
    type_name = type(structure_value).__name__
    core_name = _structure_core_name(type_name)
    if core_name == "localizedtext":
        return "canonical-structure:localizedtext"

    if isinstance(structure_value, Mapping):
        keys = {str(key).lower() for key in structure_value.keys()}
        if keys.issubset({"encoding", "locale", "text"}) and {"locale", "text"}.issubset(keys):
            return "canonical-structure:localizedtext"

    return None


def _merge_metadata_into_registered_definition(
    registry: _SchemaRegistry,
    schema_key: str,
    metadata: dict[str, Any],
) -> None:
    if not metadata or not registry.has(schema_key):
        return

    definition_name = registry.definition_name_for(schema_key)
    definition = registry.defs.get(definition_name)
    if not isinstance(definition, dict):
        return

    for key, value in metadata.items():
        definition.setdefault(key, value)


def _structure_schema_for_type(structure_type: type[Any], registry: _SchemaRegistry) -> dict[str, Any]:
    properties: dict[str, Any] = {}

    if is_dataclass(structure_type):
        vartype_overrides = _vartype_overrides_for_structure(structure_type)
        ua_type_overrides = _ua_type_overrides_for_structure(structure_type)
        for item in fields(structure_type):
            evaluated_annotation = _evaluate_generated_annotation(item.type, structure_type, item.name)
            field_schema = _schema_for_annotation(
                evaluated_annotation if evaluated_annotation is not None else item.type, registry
            )
            ua_type_override = ua_type_overrides.get(item.name)
            if ua_type_override and _is_stringish_schema(field_schema):
                field_schema = _schema_for_annotation_string(ua_type_override, registry)
            override = vartype_overrides.get(item.name)
            if override and _is_stringish_schema(field_schema):
                field_schema = _schema_for_annotation_string(override, registry)
            if _is_stringish_schema(field_schema):
                hinted_schema = _schema_from_field_runtime_hints(item, registry)
                if hinted_schema is not None:
                    field_schema = hinted_schema
            properties[item.name] = field_schema
    else:
        annotations = getattr(structure_type, "__annotations__", {})
        if isinstance(annotations, Mapping):
            for name, annotation in annotations.items():
                properties[str(name)] = _schema_for_annotation(annotation, registry)

    return {
        "type": "object",
        "title": structure_type.__name__,
        "properties": properties,
        "additionalProperties": False,
    }


def _schema_for_annotation(annotation: Any, registry: _SchemaRegistry) -> dict[str, Any]:
    forward_arg = getattr(annotation, "__forward_arg__", None)
    if isinstance(forward_arg, str) and forward_arg.strip():
        return _schema_for_annotation_string(forward_arg, registry)

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin in {list, tuple}:
        item_annotation = args[0] if args else Any
        return {"type": "array", "items": _schema_for_annotation(item_annotation, registry)}

    if origin is not None and args:
        non_none_args = [arg for arg in args if arg is not type(None)]
        if len(non_none_args) == 1 and len(non_none_args) != len(args):
            nested = _schema_for_annotation(non_none_args[0], registry)
            nested_type = nested.get("type")
            if isinstance(nested_type, str):
                nested["type"] = [nested_type, "null"]
            elif isinstance(nested_type, list) and "null" not in nested_type:
                nested["type"] = [*nested_type, "null"]
            return nested

    if isinstance(annotation, str):
        return _schema_for_annotation_string(annotation, registry)

    if isinstance(annotation, type):
        if annotation is str or _is_string_annotation_type(annotation):
            return {"type": "string"}
        if annotation is bool:
            return {"type": "boolean"}
        if _is_integer_annotation_type(annotation):
            return {"type": "integer"}
        if _is_number_annotation_type(annotation):
            return {"type": "number"}
        if _is_datetime_annotation_type(annotation):
            return {"type": "string", "format": "date-time"}
        if _is_variant_annotation_type(annotation):
            return {}
        if issubclass(annotation, Enum):
            return {"type": "string", "enum": [item.name for item in annotation]}
        if _is_structured_class(annotation):
            return _reference_or_register_structure_from_type(
                f"py-annotation:{annotation.__module__.lower()}.{annotation.__name__.lower()}",
                annotation,
                registry,
                {},
            )

    return {"type": "string"}


def _schema_for_annotation_string(annotation: str, registry: _SchemaRegistry) -> dict[str, Any]:
    normalized = annotation.strip("'\" ").strip()
    normalized = re.sub(r"^typing\.", "", normalized)
    normalized = re.sub(r"^collections\.abc\.", "", normalized)
    lowered = normalized.lower()

    if "|" in normalized:
        parts = [part.strip() for part in normalized.split("|") if part.strip()]
        if parts:
            non_none = [part for part in parts if part.lower() not in {"none", "nonetype"}]
            if len(non_none) == 1 and len(non_none) != len(parts):
                nested = _schema_for_annotation_string(non_none[0], registry)
                nested_type = nested.get("type")
                if isinstance(nested_type, str):
                    nested["type"] = [nested_type, "null"]
                elif isinstance(nested_type, list) and "null" not in nested_type:
                    nested["type"] = [*nested_type, "null"]
                return nested
            return {"anyOf": [_schema_for_annotation_string(part, registry) for part in parts]}

    if lowered.startswith("optional[") and lowered.endswith("]"):
        inner = normalized[9:-1]
        nested = _schema_for_annotation_string(inner, registry)
        nested_type = nested.get("type")
        if isinstance(nested_type, str):
            nested["type"] = [nested_type, "null"]
        elif isinstance(nested_type, list) and "null" not in nested_type:
            nested["type"] = [*nested_type, "null"]
        return nested

    if lowered.startswith("union[") and lowered.endswith("]"):
        union_body = normalized[6:-1]
        parts = [part.strip() for part in union_body.split(",") if part.strip()]
        if parts:
            return {"anyOf": [_schema_for_annotation_string(part, registry) for part in parts]}

    if lowered.endswith("[]"):
        item_token = normalized[:-2]
        return {"type": "array", "items": _schema_for_annotation_string(item_token, registry)}
    if lowered.startswith("list[") and lowered.endswith("]"):
        item_token = normalized[5:-1]
        return {"type": "array", "items": _schema_for_annotation_string(item_token, registry)}
    if lowered.startswith("sequence[") and lowered.endswith("]"):
        item_token = normalized[9:-1]
        return {"type": "array", "items": _schema_for_annotation_string(item_token, registry)}

    scalar_map: dict[str, dict[str, Any]] = {
        "ua.string": {"type": "string"},
        "string": {"type": "string"},
        "ua.euinformation": {"type": "object"},
        "euinformation": {"type": "object"},
        "ua.basedatatype": {},
        "basedatatype": {},
        "ua.boolean": {"type": "boolean"},
        "boolean": {"type": "boolean"},
        "bool": {"type": "boolean"},
        "ua.byte": {"type": "integer"},
        "ua.sbyte": {"type": "integer"},
        "ua.int16": {"type": "integer"},
        "ua.int32": {"type": "integer"},
        "ua.int64": {"type": "integer"},
        "ua.uint16": {"type": "integer"},
        "ua.uint32": {"type": "integer"},
        "ua.uint64": {"type": "integer"},
        "ua.double": {"type": "number"},
        "ua.float": {"type": "number"},
        "ua.datetime": {"type": "string", "format": "date-time"},
        "datetime": {"type": "string", "format": "date-time"},
    }
    if lowered in scalar_map:
        return scalar_map[lowered]

    structure_type = _resolve_structure_type_by_name(normalized)
    if isinstance(structure_type, type) and _is_structured_class(structure_type):
        return _reference_or_register_structure_from_type(
            f"ua-annotation:{structure_type.__module__.lower()}.{structure_type.__name__.lower()}",
            structure_type,
            registry,
            {},
        )

    imported_type = _resolve_type_from_module_path(normalized)
    if isinstance(imported_type, type) and _is_structured_class(imported_type):
        return _reference_or_register_structure_from_type(
            f"module-annotation:{imported_type.__module__.lower()}.{imported_type.__name__.lower()}",
            imported_type,
            registry,
            {},
        )

    # If this token denotes a structured datatype but cannot be resolved,
    # represent it as an object instead of degrading to string.
    simple_name = normalized.split(".")[-1]
    if simple_name.lower().endswith("datatype"):
        return {"type": "object", "title": simple_name}

    return {"type": "string"}


def _resolve_structure_type_by_name(type_name: str) -> type[Any] | None:
    candidates = [type_name.strip("'").strip()]
    if "." in candidates[0]:
        candidates.append(candidates[0].split(".")[-1])
    if candidates[0].lower().startswith("ua."):
        candidates.append(candidates[0][3:])

    for candidate in candidates:
        resolved = getattr(ua, candidate, None)
        if isinstance(resolved, type):
            return resolved

    lowered_candidates = {candidate.lower() for candidate in candidates}
    registries = [
        getattr(ua, "extension_objects_by_datatype", None),
        getattr(ua, "extension_objects_by_typeid", None),
        getattr(ua, "EXTENSION_OBJECT_CLASSES_BY_DATATYPE", None),
    ]
    for registry in registries:
        if not isinstance(registry, Mapping):
            continue
        for value in registry.values():
            if not isinstance(value, type):
                continue
            if value.__name__.lower() in lowered_candidates:
                return value

    loaded_modules: list[ModuleType] = [
        module
        for module_name, module in sys.modules.items()
        if module_name.startswith("asyncua.common.structures") and isinstance(module, ModuleType)
    ]
    for module in loaded_modules:
        for candidate_name in dir(module):
            if candidate_name.lower() in lowered_candidates:
                resolved_candidate = getattr(module, candidate_name, None)
                if isinstance(resolved_candidate, type):
                    return resolved_candidate

    return None


def _resolve_type_from_module_path(type_name: str) -> type[Any] | None:
    normalized = type_name.strip("'\" ").strip()
    if "." not in normalized:
        return None

    module_name, attr_name = normalized.rsplit(".", 1)
    try:
        module = importlib.import_module(module_name)
    except Exception:
        return None

    direct = getattr(module, attr_name, None)
    if isinstance(direct, type):
        return direct

    lowered = attr_name.lower()
    for candidate_name in dir(module):
        if candidate_name.lower() == lowered:
            candidate = getattr(module, candidate_name, None)
            if isinstance(candidate, type):
                return candidate

    return None


def _is_structured_class(candidate: type[Any]) -> bool:
    if is_dataclass(candidate):
        return True
    annotations = getattr(candidate, "__annotations__", None)
    if isinstance(annotations, Mapping) and annotations:
        return True
    return False


def _vartype_overrides_for_structure(structure_type: type[Any]) -> dict[str, str]:
    doc = getattr(structure_type, "__doc__", None)
    if not isinstance(doc, str) or not doc:
        return {}

    overrides: dict[str, str] = {}
    matches = re.findall(r":vartype\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([^\n\r]+)", doc)
    for field_name, type_token in matches:
        token = type_token.strip().strip("'\"")
        if token:
            overrides[field_name] = token
    return overrides


def _ua_type_overrides_for_structure(structure_type: type[Any]) -> dict[str, str]:
    raw = getattr(structure_type, "ua_types", None)
    if raw is None:
        return {}

    overrides: dict[str, str] = {}
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            if isinstance(key, str) and isinstance(value, str):
                token = value.strip().strip("'\"")
                if token:
                    overrides[key] = token
        return overrides

    if isinstance(raw, list):
        for item in raw:
            if not (isinstance(item, tuple) and len(item) >= 2):
                continue
            field_name = item[0]
            field_type = item[1]
            if isinstance(field_name, str) and isinstance(field_type, str):
                token = field_type.strip().strip("'\"")
                if token:
                    overrides[field_name] = token
    return overrides


def _schema_from_field_runtime_hints(field_info: Any, registry: _SchemaRegistry) -> dict[str, Any] | None:
    metadata = getattr(field_info, "metadata", None)
    if isinstance(metadata, Mapping):
        for value in metadata.values():
            candidate = _schema_from_hint_token(value, registry)
            if candidate is not None and not _is_stringish_schema(candidate):
                return candidate

    default_value = getattr(field_info, "default", MISSING)
    if default_value is not MISSING:
        candidate = _schema_from_hint_token(default_value, registry)
        if candidate is not None and not _is_stringish_schema(candidate):
            return candidate

    default_factory = getattr(field_info, "default_factory", MISSING)
    if default_factory is not MISSING:
        candidate = _schema_from_hint_token(default_factory, registry)
        if candidate is not None and not _is_stringish_schema(candidate):
            return candidate
        if callable(default_factory):
            try:
                produced = default_factory()
            except Exception:
                produced = None
            if produced is not None:
                produced_schema = _schema_for_structured_value(produced, None, None, None, [], registry)
                if produced_schema is not None and not _is_stringish_schema(produced_schema):
                    return produced_schema

    return None


def _evaluate_generated_annotation(annotation: Any, owner_type: type[Any], field_name: str | None = None) -> Any | None:
    if not isinstance(annotation, str):
        return None

    module = sys.modules.get(owner_type.__module__)
    if module is None:
        return None

    globals_dict = dict(vars(module))
    globals_dict.setdefault("typing", typing)
    globals_dict.setdefault("ua", ua)
    globals_dict.setdefault("__SELF__", owner_type)

    if field_name and "_dep_" in annotation:
        guessed_type = _guess_generated_dependency_type(field_name, owner_type)
        if guessed_type is not None:
            for dep_name in set(re.findall(r"_dep_\d+", annotation)):
                globals_dict.setdefault(dep_name, guessed_type)

    try:
        return eval(annotation, globals_dict, {})
    except Exception:
        return None


def _schema_from_hint_token(token: Any, registry: _SchemaRegistry) -> dict[str, Any] | None:
    if isinstance(token, str):
        return _schema_for_annotation_string(token, registry)
    if isinstance(token, type):
        return _schema_for_annotation(token, registry)

    if callable(token) and isinstance(getattr(token, "__name__", None), str):
        maybe_type = _resolve_structure_type_by_name(token.__name__)
        if isinstance(maybe_type, type):
            return _schema_for_annotation(maybe_type, registry)

    instance_type = type(token)
    if _is_structured_class(instance_type):
        return _schema_for_annotation(instance_type, registry)

    return None


def _is_stringish_schema(schema: dict[str, Any]) -> bool:
    schema_type = schema.get("type")
    if schema_type == "string":
        return True
    if isinstance(schema_type, list) and all(item in {"string", "null"} for item in schema_type):
        return True
    if schema_type == "array":
        items = schema.get("items")
        if isinstance(items, dict):
            items_type = items.get("type")
            if items_type == "string":
                return True
            if isinstance(items_type, list) and all(item in {"string", "null"} for item in items_type):
                return True
    return False


def _guess_generated_dependency_type(field_name: str, owner_type: type[Any]) -> type[Any] | None:
    candidates = _available_generated_structure_types(owner_type.__module__)
    if not candidates:
        return None

    preferred_stems = _preferred_field_stems(field_name)
    stems = _candidate_field_stems(field_name)
    best_score = -1
    best_type: type[Any] | None = None

    for candidate in candidates:
        core = _structure_core_name(candidate.__name__)
        score = 0
        for stem in preferred_stems:
            if stem == core:
                score = max(score, 200)
            elif core in stem:
                score = max(score, 150)
            elif stem in core:
                score = max(score, 120)
        for stem in stems:
            if stem == core:
                score = max(score, 100)
            elif core in stem:
                score = max(score, 80)
            elif stem in core:
                score = max(score, 60)
        if score > best_score:
            best_score = score
            best_type = candidate

    return best_type if best_score >= 60 else None


def _available_generated_structure_types(module_name: str) -> list[type[Any]]:
    output: list[type[Any]] = []
    seen: set[type[Any]] = set()

    module = sys.modules.get(module_name)
    if module is not None:
        for candidate_name in dir(module):
            candidate = getattr(module, candidate_name, None)
            if isinstance(candidate, type) and _is_structured_class(candidate) and candidate not in seen:
                seen.add(candidate)
                output.append(candidate)

    registries = [
        getattr(ua, "extension_objects_by_datatype", None),
        getattr(ua, "extension_objects_by_typeid", None),
        getattr(ua, "EXTENSION_OBJECT_CLASSES_BY_DATATYPE", None),
    ]
    for registry in registries:
        if not isinstance(registry, Mapping):
            continue
        for candidate in registry.values():
            if not isinstance(candidate, type) or candidate in seen:
                continue
            if candidate.__module__ == module_name or candidate.__module__.startswith("asyncua.common.structures"):
                seen.add(candidate)
                output.append(candidate)

    return output


def _candidate_field_stems(field_name: str) -> list[str]:
    parts = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\b)|[A-Z]?[a-z]+|\d+", field_name)
    joined = "".join(part.lower() for part in parts)
    lowered_parts = [part.lower() for part in parts]
    stems = {joined, *lowered_parts}
    suffixes = [
        "id",
        "ids",
        "requirements",
        "requirement",
        "parameters",
        "parameter",
        "properties",
        "property",
    ]
    for suffix in suffixes:
        if joined.endswith(suffix):
            trimmed = joined[: -len(suffix)]
            if trimmed:
                stems.add(trimmed)
    if joined.endswith("ies"):
        stems.add(joined[:-3] + "y")
    if joined.endswith("s") and len(joined) > 1:
        stems.add(joined[:-1])
    return sorted(stems, key=len, reverse=True)


def _preferred_field_stems(field_name: str) -> list[str]:
    parts = [part.lower() for part in re.findall(r"[A-Z]+(?=[A-Z][a-z]|\b)|[A-Z]?[a-z]+|\d+", field_name)]
    if not parts:
        return []

    singular_map = {
        "parameters": "parameter",
        "parameter": "parameter",
        "properties": "property",
        "property": "property",
        "requirements": None,
        "requirement": None,
        "ids": None,
        "id": None,
        "states": "state",
        "state": "state",
    }

    last = parts[-1]
    singular_value = singular_map.get(last)
    if singular_value:
        return [singular_value]

    if last in {"requirements", "requirement", "ids", "id"} and len(parts) >= 2:
        prefix = "".join(parts[:-1])
        singular_prefix = prefix[:-1] if prefix.endswith("s") else prefix
        return [prefix, singular_prefix]

    joined = "".join(parts)
    singular_joined = joined[:-1] if joined.endswith("s") else joined
    return [joined, singular_joined]


def _structure_core_name(type_name: str) -> str:
    lowered = type_name.lower()
    if lowered.startswith("isa95"):
        lowered = lowered[5:]
    if lowered.endswith("datatype"):
        lowered = lowered[:-8]
    return lowered


def _is_integer_annotation_type(annotation: type[Any]) -> bool:
    try:
        if issubclass(annotation, bool):
            return False
        if issubclass(annotation, int):
            return True
    except TypeError:
        pass
    return annotation.__name__.lower() in {
        "byte",
        "sbyte",
        "int16",
        "int32",
        "int64",
        "uint16",
        "uint32",
        "uint64",
        "uint128",
    }


def _is_number_annotation_type(annotation: type[Any]) -> bool:
    if _is_integer_annotation_type(annotation):
        return True
    try:
        if issubclass(annotation, float):
            return True
    except TypeError:
        pass
    return annotation.__name__.lower() in {"float", "double", "decimal", "decimalstring"}


def _is_string_annotation_type(annotation: type[Any]) -> bool:
    try:
        if issubclass(annotation, str):
            return True
    except TypeError:
        pass
    return annotation.__name__.lower() in {
        "string",
        "chararray",
        "bytestring",
        "xmlelement",
        "qualifiedname",
    }


def _is_datetime_annotation_type(annotation: type[Any]) -> bool:
    if annotation is datetime:
        return True
    return annotation.__name__.lower() in {"datetime", "utctime"}


def _is_variant_annotation_type(annotation: type[Any]) -> bool:
    return annotation.__name__.lower() in {"variant", "basedatatype"}


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
    return _expanded_node_id(value, namespace_infos)


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


def _inline_registered_reference(schema: dict[str, Any], registry: _SchemaRegistry) -> dict[str, Any]:
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
        return schema

    definition_name = ref.split("#/$defs/", 1)[1]
    target = registry.defs.get(definition_name)
    if not isinstance(target, dict):
        return schema

    inlined = deepcopy(target)
    for key, value in schema.items():
        if key == "$ref":
            continue
        inlined[key] = value
    return inlined


def _expand_schema_refs(schema: Any, defs: Mapping[str, Any], stack: tuple[str, ...] = ()) -> Any:
    if isinstance(schema, list):
        return [_expand_schema_refs(item, defs, stack) for item in schema]
    if not isinstance(schema, Mapping):
        return schema

    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        definition_name = ref.split("#/$defs/", 1)[1]
        if definition_name in stack:
            return dict(schema)
        target = defs.get(definition_name)
        if isinstance(target, Mapping):
            merged: dict[str, Any] = deepcopy(dict(target))
            for key, value in schema.items():
                if key == "$ref":
                    continue
                merged[key] = value
            return _expand_schema_refs(merged, defs, stack + (definition_name,))

    expanded: dict[str, Any] = {}
    for key, value in schema.items():
        expanded[key] = _expand_schema_refs(value, defs, stack)
    return expanded


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


def _def_key(node_id: str) -> str:
    return node_id.replace("~", "~0").replace("/", "~1")


def _safe_definition_name(raw_name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", raw_name)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        return "Definition"
    if cleaned[0].isdigit():
        return f"D_{cleaned}"
    return cleaned


def _definition_name(
    item: OpcUaObjectTypeInfo,
    element_ids_by_node_id: Mapping[str, str],
) -> str:
    return element_ids_by_node_id.get(item.node_id, f"urn:opcua:objecttype:{item.browse_name.lower()}")


def _expanded_node_id(node_id: str, namespace_infos: list[OpcUaNamespaceInfo]) -> str:
    if node_id.startswith("nsu="):
        return node_id

    match = re.match(r"^(?:ns=(\d+);)?([isgb]=.+)$", node_id)
    if match is None:
        return node_id

    namespace_index = int(match.group(1)) if match.group(1) is not None else 0
    identifier = match.group(2)

    # Namespace index 0 is the OPC UA standard namespace.
    if namespace_index == 0:
        return f"nsu=http://opcfoundation.org/UA/;{identifier}"

    if not (0 <= namespace_index < len(namespace_infos)):
        return node_id

    namespace_uri = namespace_infos[namespace_index].uri
    if not namespace_uri:
        return node_id

    return f"nsu={namespace_uri};{identifier}"
