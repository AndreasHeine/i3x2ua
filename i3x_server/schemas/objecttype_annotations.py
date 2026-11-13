"""Annotation-to-JSON-Schema machinery for generated OPC UA structure types.

Resolves Python/``ua`` type annotations (including forward references, string
tokens, ``vartype``/``ua_types`` doc overrides and generated ``_dep_`` aliases)
to JSON Schema fragments, and builds structure schemas from dataclasses. The
value-based schema inference lives in ``objecttype_schema.py``; that module is
imported lazily inside ``_schema_from_field_runtime_hints`` to keep the two
modules free of an import cycle. Tests and monkeypatching target this module.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import logging
import re
import sys
import typing
from collections.abc import Mapping
from dataclasses import MISSING, fields, is_dataclass
from datetime import datetime
from enum import Enum
from types import ModuleType, UnionType
from typing import Any, get_args, get_origin

from asyncua import ua
from asyncua.ua.uaprotocol_auto import Argument

from i3x_server.schemas.objecttype_registry import _SchemaRegistry

logger = logging.getLogger(__name__)

# Inert builtin names an annotation string may reference. Anything callable (``__import__``,
# ``open``, ...) is deliberately absent, so a malformed annotation cannot execute code.
_ANNOTATION_BUILTINS: dict[str, Any] = {
    name: getattr(builtins, name)
    for name in (
        "bool",
        "bytearray",
        "bytes",
        "dict",
        "float",
        "frozenset",
        "int",
        "list",
        "set",
        "str",
        "tuple",
        "type",
    )
}

_CANONICAL_STRUCTURE_CORE_KEYS: dict[str, str] = {
    "nodeid": "canonical-structure:nodeid",
    "expandednodeid": "canonical-structure:expandednodeid",
    "localizedtext": "canonical-structure:localizedtext",
}

_CANONICAL_TYPE_NAME_RULES: tuple[tuple[str, str, str], ...] = (
    ("ISA95", "DataType", "canonical-structure:isa95-datatype:"),
)


def _is_filtered_structure_field(name: str) -> bool:
    return name.strip().lower() == "encoding"


def _schema_for_annotation(annotation: Any, registry: _SchemaRegistry) -> dict[str, Any]:
    forward_arg = getattr(annotation, "__forward_arg__", None)
    if isinstance(forward_arg, str) and forward_arg.strip():
        return _schema_for_annotation_string(forward_arg, registry)

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin in {list, tuple}:
        item_annotation = args[0] if args else Any
        return {"type": "array", "items": _schema_for_annotation(item_annotation, registry)}

    if origin is typing.Union or origin is UnionType:
        non_none_args = [arg for arg in args if arg is not type(None)]
        if not non_none_args:
            return {"type": "null"}
        if len(non_none_args) == 1 and len(non_none_args) != len(args):
            # Optional[X]: one type plus None, expressed as a nullable type rather than an anyOf.
            nested = _schema_for_annotation(non_none_args[0], registry)
            nested_type = nested.get("type")
            if isinstance(nested_type, str):
                nested["type"] = [nested_type, "null"]
            elif isinstance(nested_type, list) and "null" not in nested_type:
                nested["type"] = [*nested_type, "null"]
            return nested
        # A genuine union (``Union[int, str]``) must stay a union: collapsing it to a single type -
        # or to the generic string fallback below - rejects values the server legitimately allows.
        return {"anyOf": [_schema_for_annotation(arg, registry) for arg in args]}

    if annotation is type(None):
        return {"type": "null"}

    if isinstance(annotation, str):
        return _schema_for_annotation_string(annotation, registry)

    if isinstance(annotation, type):
        if annotation is str or _is_string_annotation_type(annotation):
            return {"type": "string"}
        if annotation is bool:
            return {"type": "boolean"}
        if issubclass(annotation, Enum):
            enum_values = [item.value for item in annotation]
            if enum_values and all(isinstance(value, int) and not isinstance(value, bool) for value in enum_values):
                return {
                    "type": "integer",
                    "enum": enum_values,
                    "x-opcua-enumNames": [item.name for item in annotation],
                }
            return {"type": "string", "enum": [item.name for item in annotation]}
        if _is_integer_annotation_type(annotation):
            return {"type": "integer"}
        if _is_number_annotation_type(annotation):
            return {"type": "number"}
        if _is_datetime_annotation_type(annotation):
            return {"type": "string", "format": "date-time"}
        if _is_variant_annotation_type(annotation):
            return {}
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
    if isinstance(structure_type, type):
        if issubclass(structure_type, Enum):
            return _schema_for_annotation(structure_type, registry)
        if _is_structured_class(structure_type):
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
                # Value-based inference lives in ``objecttype_schema``; imported lazily to
                # avoid an import cycle between the two modules.
                from i3x_server.schemas.objecttype_schema import _schema_for_structured_value

                produced_schema = _schema_for_structured_value(produced, None, None, None, [], registry)
                if produced_schema is not None and not _is_stringish_schema(produced_schema):
                    return produced_schema

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


def _safe_annotation_expression(node: ast.AST, globals_dict: Mapping[str, Any]) -> Any:
    """Evaluate an annotation AST without executing code.

    Annotation strings are sourced from upstream OPC UA structure metadata, so they must never
    be executed. Only constructs a type annotation can legitimately contain are accepted:
    literals, name lookups resolved against the owner module's globals, attribute chains on
    modules/classes, subscripts of typing constructs, containers, and ``X | Y`` unions. Anything
    else - calls, lambdas, comprehensions, ``__globals__`` traversal - raises, and the caller
    degrades to ``None`` instead of running it.
    """
    if isinstance(node, ast.Expression):
        return _safe_annotation_expression(node.body, globals_dict)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in globals_dict:
            return globals_dict[node.id]
        # Annotation strings reference builtins like ``int``/``str`` the same way ``eval`` did;
        # only these inert type objects are resolvable, never ``__import__`` or other callables.
        if node.id in _ANNOTATION_BUILTINS:
            return _ANNOTATION_BUILTINS[node.id]
        raise ValueError(f"unknown annotation name {node.id!r}")
    if isinstance(node, ast.Attribute):
        base = _safe_annotation_expression(node.value, globals_dict)
        if not isinstance(base, (ModuleType, type)):
            # Attribute chains on arbitrary instances can trigger user code (properties,
            # ``__getattr__``); module and class attributes are inert lookups.
            raise ValueError("annotation attribute must start at a module or class")
        return getattr(base, node.attr)
    if isinstance(node, ast.Subscript):
        base = _safe_annotation_expression(node.value, globals_dict)
        key = _safe_annotation_expression(node.slice, globals_dict)
        return base[key]
    if isinstance(node, ast.Tuple):
        return tuple(_safe_annotation_expression(item, globals_dict) for item in node.elts)
    if isinstance(node, ast.List):
        return [_safe_annotation_expression(item, globals_dict) for item in node.elts]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _safe_annotation_expression(node.left, globals_dict) | _safe_annotation_expression(
            node.right, globals_dict
        )
    raise ValueError(f"unsupported annotation expression {type(node).__name__}")


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
        tree = ast.parse(annotation, mode="eval")
    except (SyntaxError, ValueError):
        logger.warning(
            "Generated annotation not parsed owner=%s field=%s annotation=%r",
            owner_type.__name__,
            field_name,
            annotation,
        )
        return None

    try:
        return _safe_annotation_expression(tree, globals_dict)
    except Exception:
        # An annotation the safe evaluator cannot resolve is a malformed upstream hint; it must
        # degrade to the runtime type rather than run anything the string contains.
        logger.warning(
            "Generated annotation not evaluated owner=%s field=%s annotation=%r",
            owner_type.__name__,
            field_name,
            annotation,
        )
        return None


def _structure_schema_for_type(structure_type: type[Any], registry: _SchemaRegistry) -> dict[str, Any]:
    if _is_nodeid_structure_type(structure_type):
        return _nodeid_discriminated_schema_for_type(structure_type, registry)

    properties: dict[str, Any] = {}

    if is_dataclass(structure_type):
        vartype_overrides = _vartype_overrides_for_structure(structure_type)
        ua_type_overrides = _ua_type_overrides_for_structure(structure_type)
        for item in fields(structure_type):
            if _is_filtered_structure_field(item.name):
                continue
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
                if _is_filtered_structure_field(str(name)):
                    continue
                properties[str(name)] = _schema_for_annotation(annotation, registry)

    if issubclass(structure_type, Argument):
        properties["ArrayDimensions"]["type"] = ["array", "null"]

    return {
        "type": "object",
        "title": structure_type.__name__,
        "properties": properties,
        "additionalProperties": False,
    }


def _is_nodeid_structure_type(structure_type: type[Any]) -> bool:
    return structure_type.__name__ in {"NodeId", "ExpandedNodeId"}


def _nodeid_discriminated_schema_for_type(
    structure_type: type[Any],
    registry: _SchemaRegistry,
) -> dict[str, Any]:
    base_schema = _nodeid_common_schema_for_type(structure_type, registry)
    base_schema["oneOf"] = _nodeid_identifier_branches()
    return base_schema


def _nodeid_common_schema_for_type(
    structure_type: type[Any],
    registry: _SchemaRegistry,
) -> dict[str, Any]:
    common_properties: dict[str, Any] = {}
    required_fields: list[str] = []

    annotations = getattr(structure_type, "__annotations__", {})
    if isinstance(annotations, Mapping):
        for name, annotation in annotations.items():
            if _is_filtered_structure_field(str(name)):
                continue
            if str(name) == "Identifier":
                # Identifier semantics are constrained by oneOf branches below.
                common_properties[str(name)] = {}
            else:
                common_properties[str(name)] = _schema_for_annotation(annotation, registry)
            required_fields.append(str(name))

    return {
        "type": "object",
        "title": structure_type.__name__,
        "properties": common_properties,
        "required": required_fields,
        "additionalProperties": False,
    }


def _nodeid_identifier_branches() -> list[dict[str, Any]]:
    return [
        {
            "properties": {
                "NodeIdType": {"const": 0},
                "Identifier": {"type": "integer", "minimum": 0, "maximum": 255},
            },
            "required": ["NodeIdType", "Identifier"],
        },
        {
            "properties": {
                "NodeIdType": {"const": 1},
                "Identifier": {"type": "integer", "minimum": 0, "maximum": 4294967295},
            },
            "required": ["NodeIdType", "Identifier"],
        },
        {
            "properties": {
                "NodeIdType": {"const": 2},
                "Identifier": {"type": "integer"},
            },
            "required": ["NodeIdType", "Identifier"],
        },
        {
            "properties": {
                "NodeIdType": {"const": 3},
                "Identifier": {"type": "string"},
            },
            "required": ["NodeIdType", "Identifier"],
        },
        {
            "properties": {
                "NodeIdType": {"const": 4},
                "Identifier": {"type": "string", "format": "uuid"},
            },
            "required": ["NodeIdType", "Identifier"],
        },
        {
            "properties": {
                "NodeIdType": {"const": 5},
                "Identifier": {"type": "string", "contentEncoding": "base64"},
            },
            "required": ["NodeIdType", "Identifier"],
        },
    ]


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


def _canonical_schema_key_for_type(structure_type: type[Any]) -> str | None:
    type_name = structure_type.__name__
    core_name = _structure_core_name(type_name)

    direct_key = _canonical_structure_key_for_core_name(core_name)
    if direct_key is not None:
        return direct_key

    for prefix, suffix, key_prefix in _CANONICAL_TYPE_NAME_RULES:
        if type_name.startswith(prefix) and type_name.endswith(suffix):
            return f"{key_prefix}{core_name}"

    return None


def _canonical_schema_key_for_structure_value(structure_value: Any) -> str | None:
    type_name = type(structure_value).__name__
    core_name = _structure_core_name(type_name)
    direct_key = _canonical_structure_key_for_core_name(core_name)
    if direct_key is not None:
        return direct_key

    if isinstance(structure_value, Mapping):
        keys = {str(key).lower() for key in structure_value.keys()}
        if keys.issubset({"encoding", "locale", "text"}) and {"locale", "text"}.issubset(keys):
            return _CANONICAL_STRUCTURE_CORE_KEYS["localizedtext"]

    return None


def _canonical_structure_key_for_core_name(core_name: str) -> str | None:
    return _CANONICAL_STRUCTURE_CORE_KEYS.get(core_name)


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
