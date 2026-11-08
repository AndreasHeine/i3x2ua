"""Schema definition registry and ``$ref`` post-processing for object-type schemas.

The registry keys definitions by schema key, allocates collision-free definition
names, and the post-processing helpers share equal ``$defs`` entries and expand
or inline local ``#/$defs`` references. Pure JSON-container work; no OPC UA
type resolution happens here. Tests and monkeypatching target this module.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from i3x_server.domain.ports.opcua import OpcUaObjectTypeInfo


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


def deduplicate_schema_fragments(schemas: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Share equal JSON containers within one read-only schema collection."""
    pool: dict[str, Any] = {}

    def intern(value: Any) -> Any:
        candidate: dict[str, Any] | list[Any]
        if isinstance(value, dict):
            candidate = {key: intern(item) for key, item in value.items()}
        elif isinstance(value, list):
            candidate = [intern(item) for item in value]
        else:
            return value
        # Preserve object order and distinguish JSON scalar types in the key.
        key = json.dumps(candidate, separators=(",", ":"), ensure_ascii=True)
        if key not in pool:
            pool[key] = candidate
        return pool[key]

    return [intern(schema) for schema in schemas]


def remove_opcua_schema_fields(schema: Any) -> Any:
    if isinstance(schema, Mapping):
        return {
            key: remove_opcua_schema_fields(value)
            for key, value in schema.items()
            if not str(key).startswith("x-opcua-")
        }
    if isinstance(schema, list):
        return [remove_opcua_schema_fields(item) for item in schema]
    return schema


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


def _dedupe_defs_and_rewrite_local_refs(schema: dict[str, Any]) -> None:
    defs = schema.get("$defs")
    if not isinstance(defs, Mapping):
        return

    alias_by_name: dict[str, str] = {}
    name_by_fingerprint: dict[str, str] = {}
    deduped_defs: dict[str, Any] = {}

    for name, definition in defs.items():
        definition_name = str(name)
        fingerprint = _schema_fingerprint(definition)
        winner = name_by_fingerprint.get(fingerprint)
        if winner is None:
            name_by_fingerprint[fingerprint] = definition_name
            alias_by_name[definition_name] = definition_name
            deduped_defs[definition_name] = definition
            continue
        alias_by_name[definition_name] = winner

    if all(source == target for source, target in alias_by_name.items()):
        return

    _rewrite_local_defs_refs(schema, alias_by_name)
    schema["$defs"] = deduped_defs


def _rewrite_local_defs_refs(value: Any, alias_by_name: Mapping[str, str]) -> Any:
    if isinstance(value, list):
        for index, item in enumerate(value):
            value[index] = _rewrite_local_defs_refs(item, alias_by_name)
        return value
    if not isinstance(value, dict):
        return value

    ref = value.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        definition_name = ref.split("#/$defs/", 1)[1]
        replacement = alias_by_name.get(definition_name)
        if replacement is not None:
            value["$ref"] = f"#/$defs/{replacement}"

    for key, nested in value.items():
        value[key] = _rewrite_local_defs_refs(nested, alias_by_name)
    return value


def _schema_fingerprint(value: Any) -> str:
    normalized = _normalize_schema_value(value)
    try:
        return json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except TypeError:
        return repr(normalized)


def _normalize_schema_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_schema_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list):
        return [_normalize_schema_value(item) for item in value]
    return value


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
    """Definition name for an ancestor ObjectType.

    An unresolved ancestor must be keyed by its node id, not only by BrowseName: ObjectTypes in
    different namespaces can share a BrowseName, and one of them is frequently a subtype of the other.
    A shared name makes ``set_definition`` overwrite one schema, and both ``allOf`` entries then point
    at the surviving schema - write validation would check the wrong required fields and types.
    """
    element_id = element_ids_by_node_id.get(item.node_id)
    if element_id:
        return element_id
    return f"unresolved-objecttype:{item.browse_name.lower()}-{_def_key(item.node_id)}"
