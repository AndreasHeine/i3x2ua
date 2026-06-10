from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from i3x_server.opcua.client import OpcUaNamespaceInfo, OpcUaObjectTypeInfo, OpcUaObjectTypeMemberInfo

_MANDATORY_RULES = {"mandatory", "mandatoryplaceholder"}


def json_schema_for_opcua_type(data_type: str | None) -> dict[str, Any]:
    if data_type is None:
        return {"type": "string"}

    normalized = data_type.lower()
    if normalized.endswith("i=10") or normalized.endswith("i=11"):
        return {"type": "number"}
    if any(normalized.endswith(f"i={idx}") for idx in [2, 3, 4, 5, 6, 7, 8, 9]):
        return {"type": "integer"}
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

    return {"type": "string"}


def build_object_type_schema(
    item: OpcUaObjectTypeInfo,
    object_types_by_node_id: Mapping[str, OpcUaObjectTypeInfo],
    element_ids_by_node_id: Mapping[str, str],
    namespace_infos: list[OpcUaNamespaceInfo],
) -> dict[str, Any]:
    lineage = _lineage(item, object_types_by_node_id)

    defs: dict[str, Any] = {}
    all_of: list[dict[str, str]] = []
    merged_properties: dict[str, Any] = {}
    merged_required: list[str] = []
    required_seen: set[str] = set()

    for ancestor in lineage:
        definition_key = _def_key(_definition_name(ancestor, element_ids_by_node_id))
        ancestor_schema, ancestor_required = _schema_for_single_type(ancestor, namespace_infos)
        defs[definition_key] = ancestor_schema
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
        "$defs": defs,
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
) -> tuple[dict[str, Any], list[str]]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    required_seen: set[str] = set()

    for member in _members(item):
        prop_schema = _schema_for_member(member, namespace_infos)
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


def _schema_for_member(member: OpcUaObjectTypeMemberInfo, namespace_infos: list[OpcUaNamespaceInfo]) -> dict[str, Any]:
    if member.node_class.lower() == "object":
        schema: dict[str, Any] = {"type": "object"}
    else:
        schema = json_schema_for_opcua_type(member.data_type)

    if member.modelling_rule:
        schema["x-opcua-modellingRule"] = member.modelling_rule
    schema["x-opcua-nodeId"] = _expanded_node_id(member.node_id, namespace_infos)
    schema["x-opcua-displayName"] = member.display_name
    if member.description:
        schema["description"] = member.description
        schema["x-opcua-description"] = member.description
    if member.value is not None:
        schema["x-opcua-value"] = member.value
    if member.display_name and member.display_name != member.browse_name:
        schema["title"] = member.display_name
    return schema


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
    if not (0 <= namespace_index < len(namespace_infos)):
        return node_id

    namespace_uri = namespace_infos[namespace_index].uri
    if not namespace_uri:
        return node_id

    return f"nsu={namespace_uri};{identifier}"
