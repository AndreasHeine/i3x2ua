"""JSON-schema wrapper and local ``$defs`` utilities for object type schemas.

A served type schema wraps the scalar/structure schema in a ``null | value | array``
``oneOf`` envelope and keeps only the ``$defs`` fragments a fragment actually references.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

# Built-in scalar types whose OPC UA representation may legitimately be null.
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
