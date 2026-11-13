"""Object-type loading, member metadata and type/reference hierarchy resolution.

Builds the cached ``OpcUaObjectTypeInfo`` graph (browse of the ObjectType tree plus
member metadata, values and modelling rules), resolves reference-type supertypes and
type-definition hierarchies, and converts member values to JSON-compatible metadata.
Cache lookup and coalescing stay on ``OpcUaClient`` (``client.py``); browse and attribute
plumbing go through its entry points (``browse.py`` / ``node_reading.py``), so
monkeypatching the client keeps working. Tests and monkeypatching target this module.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import fields, is_dataclass
from datetime import datetime
from time import perf_counter
from typing import TYPE_CHECKING, Any

from asyncua import ua
from asyncua.ua import NodeClass
from asyncua.ua.attribute_ids import AttributeIds
from asyncua.ua.object_ids import ObjectIds

from i3x_server.domain.ports.opcua import (
    DEFAULT_MAX_NODES_PER_BROWSE,
    DEFAULT_MAX_NODES_PER_READ,
    OpcUaObjectTypeInfo,
    OpcUaObjectTypeMemberInfo,
    OpcUaOperationalLimits,
    OpcUaTypeDefinitionInfo,
)
from i3x_server.infrastructure.opcua.batching import chunked
from i3x_server.infrastructure.opcua.browse import _gather_cancelled_on_failure
from i3x_server.infrastructure.opcua.node_reading import _normalize_type_definition_id

if TYPE_CHECKING:
    from i3x_server.infrastructure.opcua.client import OpcUaClient

logger = logging.getLogger(__name__)


def _reference_type_name(reference_type_id: str | None) -> str | None:
    if not isinstance(reference_type_id, str) or not reference_type_id:
        return None
    match = re.match(r"^(?:ns=0;|nsu=http://opcfoundation.org/UA/;)i=(\d+)$", reference_type_id, flags=re.IGNORECASE)
    if match is None:
        return None
    identifier = int(match.group(1))
    object_id_names = getattr(ua, "ObjectIdNames", None)
    if not isinstance(object_id_names, dict):
        return None
    candidate = object_id_names.get(identifier)
    if not isinstance(candidate, str) or not candidate:
        return None
    return candidate


def _to_json_compatible(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        # Raw ByteString members must use the same base64 convention as every other byte payload
        # in this gateway (write path ``history.py``/``common_helpers.py``); ``str(value)`` would
        # publish a Python repr such as ``"b'\\xff\\x00'"``.
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return {item.name: _to_json_compatible(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, (list, tuple)):
        return [_to_json_compatible(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_json_compatible(item) for key, item in value.items()}
    if hasattr(value, "__dict__") and type(value).__module__ != "builtins":
        return {
            str(key): _to_json_compatible(item)
            for key, item in vars(value).items()
            if not key.startswith("_") and not callable(item)
        }
    return str(value)


def _variant_value_or_self(data_value: Any) -> Any:
    if data_value is None:
        return None
    variant = getattr(data_value, "Value", None)
    if variant is None:
        return data_value
    return getattr(variant, "Value", variant)


def _variant_metadata(data_value: Any) -> tuple[str | None, bool | None, list[int] | None]:
    if data_value is None:
        return None, None, None
    variant = getattr(data_value, "Value", None)
    if variant is None:
        return None, None, None

    variant_type = getattr(variant, "VariantType", None)
    variant_type_name = None
    if variant_type is not None:
        variant_type_name = str(getattr(variant_type, "name", variant_type))

    is_array = None
    array_dimensions: list[int] | None = None
    array_type = getattr(variant, "is_array", None)
    if callable(array_type):
        try:
            is_array = bool(array_type())
        except Exception:
            is_array = None
    dims = getattr(variant, "Dimensions", None)
    if isinstance(dims, (list, tuple)) and dims:
        normalized_dims: list[int] = []
        for dim in dims:
            try:
                normalized_dims.append(int(dim))
            except (TypeError, ValueError):
                continue
        if normalized_dims:
            array_dimensions = normalized_dims
    if is_array is None:
        if array_dimensions:
            is_array = True
    if is_array is None:
        candidate_value = getattr(variant, "Value", None)
        if isinstance(candidate_value, (list, tuple)):
            is_array = True

    return variant_type_name, is_array, array_dimensions


async def resolve_reference_type_supertype_browse_names(client: OpcUaClient, reference_type_id: str) -> list[str]:
    if not isinstance(reference_type_id, str) or not reference_type_id:
        return []
    return await client.resolve_reference_type_chain(reference_type_id, set())


async def resolve_type_definition_hierarchy(
    client: OpcUaClient,
    type_definition_id: str,
) -> list[OpcUaTypeDefinitionInfo]:
    if not isinstance(type_definition_id, str) or not type_definition_id:
        return []
    hierarchies = await client.resolve_type_definition_hierarchies([type_definition_id])
    return hierarchies[type_definition_id]


async def resolve_type_definition_hierarchies(
    client: OpcUaClient,
    type_definition_ids: list[str],
) -> dict[str, list[OpcUaTypeDefinitionInfo]]:
    started = perf_counter()
    requested_ids = list(dict.fromkeys(type_definition_ids))
    pending = requested_ids
    cached_chains: dict[str, list[OpcUaTypeDefinitionInfo]] = {}
    type_infos: dict[str, OpcUaTypeDefinitionInfo] = {}
    parents_by_id: dict[str, list[str]] = {}
    cache_hits = 0
    if not requested_ids:
        return {}

    limits: OpcUaOperationalLimits | None = None
    while pending:
        unresolved: list[str] = []
        for type_id in dict.fromkeys(pending):
            if type_id in parents_by_id or type_id in cached_chains:
                continue
            cached_chain = client.cached_type_definition_hierarchy(type_id)
            if cached_chain is not None:
                cached_chains[type_id] = list(cached_chain)
                cache_hits += 1
            else:
                unresolved.append(type_id)
        if not unresolved:
            break
        if limits is None:
            limits = await client.get_operational_limits()
        nodes = [client.raw_node(type_id) for type_id in unresolved]
        for node_batch in chunked(nodes, limits.max_nodes_per_read or DEFAULT_MAX_NODES_PER_READ):
            values = await client.read_attribute_batch(node_batch, AttributeIds.BrowseName)
            for node, value in zip(node_batch, values, strict=True):
                type_id = node.nodeid.to_string()
                qualified_name = client.extract_attribute_value(value)
                if not isinstance(qualified_name, ua.QualifiedName) or not qualified_name.Name:
                    raise ValueError(f"Cannot resolve type BrowseName for {type_id}: {value.StatusCode}")
                type_infos[type_id] = OpcUaTypeDefinitionInfo(type_id, qualified_name.Name)
        browsed = await client.browse_references_descriptions(
            nodes,
            max_nodes_per_browse=limits.max_nodes_per_browse or DEFAULT_MAX_NODES_PER_BROWSE,
            reference_type_id=ObjectIds.HasSubtype,
            browse_direction=ua.BrowseDirection.Inverse,
            include_subtypes=False,
        )
        pending = []
        for node, refs in browsed:
            parents = list(dict.fromkeys(ref.NodeId.to_string() for ref in refs))
            parents_by_id[node.nodeid.to_string()] = parents
            pending.extend(parents)

    hierarchies = dict(cached_chains)
    for root_id in parents_by_id:
        hierarchy: list[OpcUaTypeDefinitionInfo] = []
        seen_ids: set[str] = set()
        stack = [root_id]
        while stack:
            type_id = stack.pop()
            if type_id in seen_ids:
                continue
            if type_id in cached_chains:
                for info in cached_chains[type_id]:
                    if info.node_id not in seen_ids:
                        seen_ids.add(info.node_id)
                        hierarchy.append(info)
            else:
                seen_ids.add(type_id)
                hierarchy.append(type_infos[type_id])
                stack.extend(reversed(parents_by_id[type_id]))
        hierarchies[root_id] = hierarchy
        client.store_type_definition_hierarchy(root_id, list(hierarchy))
    logger.info(
        "OPC UA type hierarchy resolution endpoint=%s requested=%d cache_hits=%d resolved_types=%d duration_s=%.3f",
        client.endpoint,
        len(requested_ids),
        cache_hits,
        len(type_infos),
        perf_counter() - started,
    )
    return {type_id: list(hierarchies[type_id]) for type_id in requested_ids}


async def resolve_reference_type_chain(client: OpcUaClient, type_id: str, in_progress: set[str]) -> list[str]:
    """Browse names of ``type_id`` followed by its supertypes, nearest ancestor first.

    Every node reached is cached in its own right, so sibling reference types that
    converge on shared ancestors resolve those ancestors only once.
    """
    cached_names = client.cached_reference_type_supertypes(type_id)
    if cached_names is not None:
        return list(cached_names)

    if type_id in in_progress:
        return []
    in_progress.add(type_id)

    discovered_names: list[str] = []
    seen_names: set[str] = set()
    type_node = client.raw_node(type_id)

    try:
        browse_name_obj = await type_node.read_browse_name()
        browse_name = getattr(browse_name_obj, "Name", None)
        if isinstance(browse_name, str) and browse_name:
            seen_names.add(browse_name)
            discovered_names.append(browse_name)
    except Exception:
        logger.debug(
            "OPC UA browse name read failed for reference type endpoint=%s reference_type_id=%s",
            client.endpoint,
            type_id,
            exc_info=True,
        )

    try:
        supertype_refs_by_node = await client.browse_references_descriptions(
            [type_node],
            max_nodes_per_browse=1,
            reference_type_id=ObjectIds.HasSubtype,
            browse_direction=ua.BrowseDirection.Inverse,
            include_subtypes=False,
        )
    except Exception:
        logger.debug(
            "OPC UA supertype browse failed endpoint=%s reference_type_id=%s",
            client.endpoint,
            type_id,
            exc_info=True,
        )
        supertype_refs_by_node = []

    for _, refs in supertype_refs_by_node:
        for ref in refs:
            supertype_id = ref.NodeId.to_string()
            if not isinstance(supertype_id, str) or not supertype_id:
                continue
            for name in await client.resolve_reference_type_chain(supertype_id, in_progress):
                if name not in seen_names:
                    seen_names.add(name)
                    discovered_names.append(name)

    in_progress.discard(type_id)
    client.store_reference_type_supertypes(type_id, list(discovered_names))
    return list(discovered_names)


async def _load_object_types(client: OpcUaClient) -> list[OpcUaObjectTypeInfo]:
    now = perf_counter()
    if client.object_type_cache is not None:
        cached_at, cached_value = client.object_type_cache
        if client.is_metadata_cache_entry_fresh(cached_at):
            logger.debug(
                "OPC UA object types cache hit endpoint=%s age_s=%.3f",
                client.endpoint,
                now - cached_at,
            )
            return cached_value

    started = perf_counter()
    limits = await client.get_operational_limits()
    max_nodes_per_browse = limits.max_nodes_per_browse or DEFAULT_MAX_NODES_PER_BROWSE

    root = client.raw_client.nodes.object_types
    root_node_id = root.nodeid.to_string()
    discovered: dict[str, tuple[str | None, str, str, Any]] = {}
    properties_by_type: dict[str, dict[str, str | None]] = {}
    members_by_type: dict[str, list[OpcUaObjectTypeMemberInfo]] = {}
    stack: list[tuple[Any, str | None]] = [(root, None)]
    visited: set[str] = set()

    try:
        while stack:
            batch_entries = stack[:max_nodes_per_browse]
            stack = stack[max_nodes_per_browse:]

            nodes = [node for node, _ in batch_entries]
            member_candidates: list[tuple[str, str, str, str, NodeClass, Any, str | None, str | None, int | None]] = []

            browsed = await client.browse_children_descriptions(nodes, max_nodes_per_browse)
            for parent_node, refs in browsed:
                parent_node_id = parent_node.nodeid.to_string()
                for ref_index, ref in enumerate(refs):
                    child_node_id = ref.NodeId.to_string()
                    if child_node_id in visited:
                        continue
                    if ref.NodeClass == NodeClass.ObjectType:
                        visited.add(child_node_id)
                        child_node = client.raw_node(ref.NodeId)
                        stack.append((child_node, parent_node_id))

                        browse_name = ref.BrowseName.Name
                        display_name = ref.DisplayName.Text or browse_name
                        discovered[child_node_id] = (parent_node_id, browse_name, display_name, child_node)
                        continue

                    # Collect direct declarations from ObjectTypes for generic schema projection.
                    if parent_node_id != root_node_id and ref.NodeClass in {NodeClass.Variable, NodeClass.Object}:
                        child_node = client.raw_node(ref.NodeId)
                        property_name = ref.BrowseName.Name or child_node_id
                        display_name = ref.DisplayName.Text or property_name
                        reference_type_id = ref.ReferenceTypeId.to_string()
                        reference_type = _reference_type_name(reference_type_id)
                        member_candidates.append(
                            (
                                parent_node_id,
                                child_node_id,
                                property_name,
                                display_name,
                                ref.NodeClass,
                                child_node,
                                reference_type_id,
                                reference_type,
                                ref_index,
                            )
                        )

            if member_candidates:
                member_infos = await client.read_object_type_members_limited(member_candidates)
                for parent_id, member_info in member_infos:
                    if member_info.node_class.lower() == "variable":
                        properties = properties_by_type.setdefault(parent_id, {})
                        properties[member_info.browse_name] = member_info.data_type

                    members = members_by_type.setdefault(parent_id, [])
                    members.append(member_info)

        for type_node_id, members in members_by_type.items():
            # Keep response stable when servers expose duplicate declaration names.
            unique_by_name = {member.browse_name: member for member in members}
            members_by_type[type_node_id] = list(unique_by_name.values())

        metadata_by_node_id = await client.read_object_type_metadata([node for _, _, _, node in discovered.values()])

        output = [
            OpcUaObjectTypeInfo(
                node_id=node_id,
                parent_node_id=parent_node_id,
                browse_name=browse_name,
                display_name=display_name,
                description=metadata_by_node_id.get(node_id, {}).get("description"),
                is_abstract=metadata_by_node_id.get(node_id, {}).get("is_abstract"),
                properties=properties_by_type.get(node_id, {}),
                members=members_by_type.get(node_id, []),
            )
            for node_id, (parent_node_id, browse_name, display_name, _) in discovered.items()
        ]

        logger.debug(
            "OPC UA object types read ok endpoint=%s count=%d duration_s=%.3f",
            client.endpoint,
            len(output),
            perf_counter() - started,
        )
        client.object_type_cache = (perf_counter(), output)
        client.runtime_metrics.object_type_reads += 1
        client.runtime_metrics.object_type_count_last = len(output)
        return output
    except Exception:
        logger.exception(
            "OPC UA object types read failed endpoint=%s duration_s=%.3f",
            client.endpoint,
            perf_counter() - started,
        )
        raise


async def read_data_type_or_none(client: OpcUaClient, node: Any) -> str | None:
    try:
        data_type_obj = await node.read_data_type()
        data_type = data_type_obj.to_string()
        if isinstance(data_type, str):
            return data_type
        return str(data_type)
    except Exception:
        logger.debug(
            "OPC UA variable datatype read failed endpoint=%s node_id=%s",
            client.endpoint,
            node.nodeid.to_string(),
            exc_info=True,
        )
        return None


async def read_object_type_members_limited(
    client: OpcUaClient,
    entries: list[tuple[str, str, str, str, NodeClass, Any, str | None, str | None, int | None]],
) -> list[tuple[str, OpcUaObjectTypeMemberInfo]]:
    if not entries:
        return []

    all_nodes = [node for _, _, _, _, _, node, _, _, _ in entries]
    all_node_ids = [node.nodeid.to_string() for node in all_nodes]

    description_by_node_id: dict[str, str | None] = {}
    descriptions_batch_ok = True

    variable_nodes = [node for _, _, _, _, node_class, node, _, _, _ in entries if node_class == NodeClass.Variable]
    variable_node_ids = [node.nodeid.to_string() for node in variable_nodes]
    object_nodes = [node for _, _, _, _, node_class, node, _, _, _ in entries if node_class != NodeClass.Variable]

    data_type_by_node_id: dict[str, str | None] = {}
    data_type_batch_ok = True

    value_rank_by_node_id: dict[str, int | None] = {}
    value_rank_batch_ok = True
    value_data_by_node_id: dict[str, Any] = {}
    value_batch_ok = True
    member_attributes = [(node, AttributeIds.Description) for node in all_nodes]
    member_attributes.extend((node, AttributeIds.DataType) for node in variable_nodes)
    member_attributes.extend((node, AttributeIds.ValueRank) for node in variable_nodes)
    member_attributes.extend((node, AttributeIds.Value) for node in variable_nodes)
    try:
        member_values = await client.read_attribute_pairs(member_attributes)
    except Exception:
        descriptions_batch_ok = False
        data_type_batch_ok = False
        value_rank_batch_ok = False
        value_batch_ok = False
        logger.debug(
            "OPC UA member metadata batch read failed endpoint=%s",
            client.endpoint,
            exc_info=True,
        )
    else:
        offset = 0
        description_values = member_values[offset : offset + len(all_nodes)]
        for node_id, description_value in zip(all_node_ids, description_values, strict=True):
            description_raw = client.extract_attribute_value(description_value)
            text_value = getattr(description_raw, "Text", None)
            description_by_node_id[node_id] = text_value if isinstance(text_value, str) and text_value.strip() else None
        offset += len(all_nodes)

        data_type_values = member_values[offset : offset + len(variable_nodes)]
        offset += len(variable_nodes)
        for node_id, data_type_value in zip(variable_node_ids, data_type_values, strict=True):
            data_type_raw = client.extract_attribute_value(data_type_value)
            if data_type_raw is not None:
                data_type_by_node_id[node_id] = (
                    data_type_raw.to_string() if hasattr(data_type_raw, "to_string") else str(data_type_raw)
                )

        value_rank_values = member_values[offset : offset + len(variable_nodes)]
        for node_id, value_rank_value in zip(variable_node_ids, value_rank_values, strict=True):
            value_rank_raw = client.extract_attribute_value(value_rank_value)
            if value_rank_raw is None:
                continue
            if isinstance(value_rank_raw, int):
                value_rank_by_node_id[node_id] = value_rank_raw
            else:
                try:
                    value_rank_by_node_id[node_id] = int(value_rank_raw)
                except (TypeError, ValueError):
                    value_rank_by_node_id[node_id] = None

        offset += len(variable_nodes)
        value_values = member_values[offset : offset + len(variable_nodes)]
        for node_id, data_value in zip(variable_node_ids, value_values, strict=True):
            value_data_by_node_id[node_id] = data_value

    # Batched reference browses: HasModellingRule for every member and HasTypeDefinition
    # for object members, one multi-node Browse per chunk instead of one browse per node.
    limits = await client.get_operational_limits()
    max_nodes_per_browse = limits.max_nodes_per_browse or DEFAULT_MAX_NODES_PER_BROWSE

    modelling_rule_by_node_id: dict[str, str | None] = {}
    modelling_rules_batch_ok = True
    try:
        browsed_rules = await client.browse_references_descriptions(
            all_nodes,
            max_nodes_per_browse=max_nodes_per_browse,
            reference_type_id=ObjectIds.HasModellingRule,
        )
        for node, refs in browsed_rules:
            rule_name = refs[0].BrowseName.Name if refs else None
            if isinstance(rule_name, str) and rule_name:
                modelling_rule_by_node_id[node.nodeid.to_string()] = rule_name
    except Exception:
        modelling_rules_batch_ok = False
        logger.debug(
            "OPC UA member modelling rule batch browse failed endpoint=%s",
            client.endpoint,
            exc_info=True,
        )

    type_definition_by_node_id: dict[str, str | None] = {}
    type_definitions_batch_ok = True
    if object_nodes:
        try:
            browsed_definitions = await client.browse_references_descriptions(
                object_nodes,
                max_nodes_per_browse=max_nodes_per_browse,
                reference_type_id=ObjectIds.HasTypeDefinition,
            )
            for node, refs in browsed_definitions:
                if refs:
                    type_definition_by_node_id[node.nodeid.to_string()] = _normalize_type_definition_id(
                        refs[0].NodeId.to_string()
                    )
        except Exception:
            type_definitions_batch_ok = False
            logger.debug(
                "OPC UA member type definition batch browse failed endpoint=%s",
                client.endpoint,
                exc_info=True,
            )

    semaphore = client.read_slot()

    async def worker(
        parent_id: str,
        node_id: str,
        browse_name: str,
        display_name: str,
        node_class: NodeClass,
        node: Any,
        reference_type_id: str | None,
        reference_type: str | None,
        reference_order: int | None,
    ) -> tuple[str, OpcUaObjectTypeMemberInfo]:
        async with semaphore:
            data_type: str | None = None
            if node_class == NodeClass.Variable:
                if data_type_batch_ok:
                    data_type = data_type_by_node_id.get(node_id)
                else:
                    data_type = await client.read_data_type_or_none(node)
                type_definition_id = None
            else:
                if type_definitions_batch_ok:
                    type_definition_id = type_definition_by_node_id.get(node_id)
                else:
                    type_definition_id = await client.read_type_definition_id_or_none(node)

            if node_class == NodeClass.Variable:
                if value_batch_ok:
                    data_value = value_data_by_node_id.get(node_id)
                else:
                    data_value = await client.read_data_value_or_none(node)
            else:
                data_value = None
            value = _variant_value_or_self(data_value)
            variant_type, is_array, array_dimensions = _variant_metadata(data_value)
            if node_class == NodeClass.Variable:
                if value_rank_batch_ok:
                    value_rank = value_rank_by_node_id.get(node_id)
                else:
                    value_rank = await client.read_value_rank_or_none(node)
            else:
                value_rank = None
            if value_rank is not None and value_rank >= 1:
                is_array = True
            if modelling_rules_batch_ok:
                modelling_rule = modelling_rule_by_node_id.get(node_id)
            else:
                modelling_rule = await client.read_modelling_rule_or_none(node)
            if descriptions_batch_ok:
                description = description_by_node_id.get(node_id)
            else:
                description = await client.read_description_or_none(node)
            return (
                parent_id,
                OpcUaObjectTypeMemberInfo(
                    node_id=node_id,
                    browse_name=browse_name,
                    display_name=display_name,
                    description=description,
                    node_class=node_class.name,
                    data_type=data_type,
                    value=_to_json_compatible(value),
                    schema_value=data_value if data_value is not None else value,
                    variant_type=variant_type,
                    is_array=is_array,
                    value_rank=value_rank,
                    array_dimensions=array_dimensions,
                    parent_node_id=parent_id,
                    type_definition_id=type_definition_id,
                    reference_type_id=reference_type_id,
                    reference_type=reference_type,
                    reference_order=reference_order,
                    modelling_rule=modelling_rule,
                ),
            )

    return await _gather_cancelled_on_failure(
        [
            worker(
                parent_id,
                node_id,
                browse_name,
                display_name,
                node_class,
                node,
                reference_type_id,
                reference_type,
                reference_order,
            )
            for (
                parent_id,
                node_id,
                browse_name,
                display_name,
                node_class,
                node,
                reference_type_id,
                reference_type,
                reference_order,
            ) in entries
        ]
    )


async def read_type_definition_id_or_none(client: OpcUaClient, node: Any) -> str | None:
    try:
        refs = await node.get_references(refs=ObjectIds.HasTypeDefinition, direction=ua.BrowseDirection.Forward)
        if not refs:
            return None
        return _normalize_type_definition_id(refs[0].NodeId.to_string())
    except Exception:
        logger.debug(
            "OPC UA type definition read failed endpoint=%s node_id=%s",
            client.endpoint,
            node.nodeid.to_string(),
            exc_info=True,
        )
        return None


async def read_modelling_rule_or_none(client: OpcUaClient, node: Any) -> str | None:
    try:
        browsed = await client.browse_references_descriptions(
            [node],
            max_nodes_per_browse=1,
            reference_type_id=ObjectIds.HasModellingRule,
        )
        if not browsed:
            return None

        _, refs = browsed[0]
        if not refs:
            return None

        rule_name = refs[0].BrowseName.Name
        if not isinstance(rule_name, str):
            return None
        return rule_name or None
    except Exception:
        logger.debug(
            "OPC UA modelling rule read failed endpoint=%s node_id=%s",
            client.endpoint,
            node.nodeid.to_string(),
            exc_info=True,
        )
        return None


async def read_description_or_none(client: OpcUaClient, node: Any) -> str | None:
    try:
        localized = await node.read_description()
        text = getattr(localized, "Text", None)
        if isinstance(text, str) and text.strip():
            return text
        return None
    except Exception:
        logger.debug(
            "OPC UA description read failed endpoint=%s node_id=%s",
            client.endpoint,
            node.nodeid.to_string(),
            exc_info=True,
        )
        return None


async def read_data_value_or_none(client: OpcUaClient, node: Any) -> Any:
    try:
        return await node.read_data_value()
    except Exception:
        logger.debug(
            "OPC UA data value read failed endpoint=%s node_id=%s",
            client.endpoint,
            node.nodeid.to_string(),
            exc_info=True,
        )
        return None


async def read_value_rank_or_none(client: OpcUaClient, node: Any) -> int | None:
    try:
        value_rank = await node.read_value_rank()
        if isinstance(value_rank, int):
            return value_rank
        return int(value_rank)
    except Exception:
        logger.debug(
            "OPC UA value rank read failed endpoint=%s node_id=%s",
            client.endpoint,
            node.nodeid.to_string(),
            exc_info=True,
        )
        return None


async def read_object_type_metadata(client: OpcUaClient, nodes: list[Any]) -> dict[str, dict[str, Any]]:
    if not nodes:
        return {}

    attributes = [(node, attr) for node in nodes for attr in (AttributeIds.Description, AttributeIds.IsAbstract)]
    values = await client.read_attribute_pairs(attributes)

    output: dict[str, dict[str, Any]] = {}
    for index, node in enumerate(nodes):
        node_id = node.nodeid.to_string()
        description_raw = client.extract_attribute_value(values[index * 2])
        abstract_raw = client.extract_attribute_value(values[index * 2 + 1])

        description: str | None = None
        text_value = getattr(description_raw, "Text", None)
        if isinstance(text_value, str) and text_value.strip():
            description = text_value

        is_abstract: bool | None = None
        if abstract_raw is not None:
            is_abstract = bool(abstract_raw)

        output[node_id] = {"description": description, "is_abstract": is_abstract}

    return output
