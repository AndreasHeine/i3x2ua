"""Node attribute reading and attribute-batch plumbing for the OPC UA adapter.

Reads the node-info attribute set (names, class, class-specific attributes) in
server-sized batches with a per-node fallback, and drives the raw Read service calls
with reconnect retries and ``BadTooManyOperations`` splitting. Post-reconnect waits and
error classification go through the client's entry points, which delegate to
``recovery.py``, so monkeypatching the client keeps working. Tests and monkeypatching
target this module.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from asyncua import ua
from asyncua.ua import NodeClass
from asyncua.ua.attribute_ids import AttributeIds
from asyncua.ua.object_ids import ObjectIds

from i3x_server.domain.ports.opcua import OpcUaNodeInfo, OpcUaReferenceInfo
from i3x_server.infrastructure.opcua.browse import _chunked_nodes, _gather_cancelled_on_failure

if TYPE_CHECKING:
    from i3x_server.infrastructure.opcua.client import OpcUaClient

logger = logging.getLogger(__name__)

_NODE_INFO_ATTRIBUTES = (
    AttributeIds.BrowseName,
    AttributeIds.DisplayName,
    AttributeIds.NodeClass,
    AttributeIds.Description,
    AttributeIds.DataType,
    AttributeIds.AccessLevel,
    AttributeIds.UserAccessLevel,
    AttributeIds.EventNotifier,
    AttributeIds.Executable,
    AttributeIds.UserExecutable,
)
_NODE_INFO_CLASS_ATTRIBUTES = {
    NodeClass.Variable: (
        AttributeIds.DataType,
        AttributeIds.AccessLevel,
        AttributeIds.UserAccessLevel,
    ),
    NodeClass.Object: (AttributeIds.EventNotifier,),
    NodeClass.View: (AttributeIds.EventNotifier,),
    NodeClass.Method: (AttributeIds.Executable, AttributeIds.UserExecutable),
}


def _normalize_type_definition_id(type_definition_id: str | None) -> str | None:
    if not type_definition_id:
        return None
    normalized = type_definition_id.strip()
    if normalized in {"i=0", "ns=0;i=0", "nsu=http://opcfoundation.org/UA/;i=0"}:
        return None
    return normalized


def _coerce_access_level(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_attribute_value(value: ua.DataValue) -> Any | None:
    status_code = value.StatusCode
    if status_code is not None and not status_code.is_good():
        return None
    if value.Value is None:
        return None
    return value.Value.Value


def _coerce_node_class(value: Any) -> NodeClass | None:
    if isinstance(value, NodeClass):
        return value
    if value is None:
        return None
    try:
        return NodeClass(int(value))
    except (TypeError, ValueError):
        return None


async def _read_reference_type_browse_names(
    client: OpcUaClient,
    refs: list[ua.ReferenceDescription],
    names: dict[str, str],
    inverse_names: dict[str, str] | None = None,
) -> None:
    type_ids = sorted({ref.ReferenceTypeId.to_string() for ref in refs} - names.keys())
    if not type_ids:
        return
    nodes = [client._client.get_node(type_id) for type_id in type_ids]
    values = await client._read_attribute_batch_limited(nodes, AttributeIds.BrowseName)
    for type_id, value in zip(type_ids, values, strict=True):
        qualified_name = client._extract_attribute_value(value)
        browse_name = getattr(qualified_name, "Name", None)
        if not isinstance(browse_name, str) or not browse_name:
            raise ValueError(f"Cannot read BrowseName for reference type {type_id!r}")
        names[type_id] = browse_name
    if inverse_names is None:
        return
    # The ``InverseName`` attribute (OPC UA Part 3) carries the address space's own inverse name
    # (e.g. ``HasNotifier`` -> ``NotifierOf``). Servers may leave it unset or reject the read; the
    # model build must not fail on it - the builder then falls back to its per-pair derivation.
    try:
        inverse_values = await client._read_attribute_batch_limited(nodes, AttributeIds.InverseName)
    except Exception:
        logger.warning(
            "OPC UA reference type inverse names unavailable endpoint=%s type_count=%d",
            client._endpoint,
            len(type_ids),
            exc_info=True,
        )
        return
    for type_id, value in zip(type_ids, inverse_values, strict=True):
        inverse_obj = client._extract_attribute_value(value)
        # The attribute is ``LocalizedText`` (OPC UA Part 3); some servers publish a bare string.
        inverse_name = getattr(inverse_obj, "Text", inverse_obj)
        if isinstance(inverse_name, str) and inverse_name:
            inverse_names[type_id] = inverse_name


def _to_reference_infos(
    refs: list[ua.ReferenceDescription],
    reference_type_names: dict[str, str],
    reference_type_inverse_names: dict[str, str] | None = None,
) -> list[OpcUaReferenceInfo]:
    output: list[OpcUaReferenceInfo] = []
    inverse_names = reference_type_inverse_names or {}
    for ref in refs:
        target_node_id = ref.NodeId.to_string()
        reference_type_id = ref.ReferenceTypeId.to_string()
        reference_browse_name = reference_type_names[reference_type_id]
        output.append(
            OpcUaReferenceInfo(
                target_node_id=target_node_id,
                reference_type_id=reference_type_id,
                reference_browse_name=reference_browse_name,
                reference_inverse_name=inverse_names.get(reference_type_id),
            )
        )
    return output


async def _read_node_info(client: OpcUaClient, node: Any, parent_node_id: str | None) -> OpcUaNodeInfo:
    node_id = node.nodeid.to_string()
    attribute_values = await client._read_attribute_pairs([(node, attr) for attr in _NODE_INFO_ATTRIBUTES])
    values_by_attribute = dict(zip(_NODE_INFO_ATTRIBUTES, attribute_values, strict=True))
    browse_name_obj = client._extract_attribute_value(values_by_attribute[AttributeIds.BrowseName])
    display_name_obj = client._extract_attribute_value(values_by_attribute[AttributeIds.DisplayName])
    node_class_value = client._extract_attribute_value(values_by_attribute[AttributeIds.NodeClass])
    if browse_name_obj is None or display_name_obj is None:
        raise ValueError(f"Cannot read required node names for OPC UA node {node_id}")
    node_class_obj = client._coerce_node_class(node_class_value)
    if node_class_obj is None:
        raise ValueError(f"Cannot read node class for OPC UA node {node_id}")
    description_value = client._extract_attribute_value(values_by_attribute[AttributeIds.Description])
    description_text = getattr(description_value, "Text", None)
    description = description_text if isinstance(description_text, str) and description_text.strip() else None
    class_values = {
        attr: client._extract_class_node_info_attribute(
            node_id,
            node_class_obj,
            attr,
            values_by_attribute[attr],
        )
        for attr in _NODE_INFO_CLASS_ATTRIBUTES.get(node_class_obj, ())
    }
    data_type_value = class_values.get(AttributeIds.DataType)
    data_type = (
        data_type_value.to_string()
        if data_type_value is not None and hasattr(data_type_value, "to_string")
        else (str(data_type_value) if data_type_value is not None else None)
    )
    access_level = _coerce_access_level(class_values.get(AttributeIds.AccessLevel))
    user_access_level = _coerce_access_level(class_values.get(AttributeIds.UserAccessLevel))
    event_notifier = bool(class_values.get(AttributeIds.EventNotifier, False))
    executable_value = class_values.get(AttributeIds.Executable)
    user_executable_value = class_values.get(AttributeIds.UserExecutable)
    type_definition_id: str | None = None
    if node_class_obj in {NodeClass.Object, NodeClass.Variable}:
        try:
            type_definition_obj = await node.read_type_definition()
            type_definition_id = _normalize_type_definition_id(type_definition_obj.to_string())
        except Exception:
            logger.warning(
                "OPC UA type definition read failed endpoint=%s node_id=%s",
                client._endpoint,
                node_id,
                exc_info=True,
            )

    return OpcUaNodeInfo(
        node_id=node_id,
        parent_node_id=parent_node_id,
        browse_name=browse_name_obj.Name,
        display_name=display_name_obj.Text,
        node_class=node_class_obj.name,
        data_type=data_type,
        type_definition_id=type_definition_id,
        event_notifier=event_notifier,
        description=description,
        access_level=access_level,
        user_access_level=user_access_level,
        executable=executable_value if isinstance(executable_value, bool) else None,
        user_executable=user_executable_value if isinstance(user_executable_value, bool) else None,
    )


async def _read_node_infos_limited(client: OpcUaClient, entries: list[tuple[Any, str | None]]) -> list[OpcUaNodeInfo]:
    if not entries:
        return []

    limits = await client.get_operational_limits()
    max_nodes = limits.max_nodes_per_read or len(entries)
    batch_size = max(1, min(max_nodes // len(_NODE_INFO_ATTRIBUTES), len(entries)))
    # Keep the caller's browse order stable: batch and fallback results fill
    # positional slots instead of appending fallbacks after all batches (L10).
    index_by_node_id = {node.nodeid.to_string(): index for index, (node, _parent) in enumerate(entries)}
    slots: list[OpcUaNodeInfo | None] = [None] * len(entries)

    for entry_batch in _chunked_nodes(entries, batch_size):
        nodes = [node for node, _ in entry_batch]
        parent_by_node_id = {node.nodeid.to_string(): parent_node_id for node, parent_node_id in entry_batch}

        try:
            node_attributes = [(node, attr) for node in nodes for attr in _NODE_INFO_ATTRIBUTES]
            attribute_values = await client._read_attribute_pairs(node_attributes)
        except Exception:
            logger.warning(
                "OPC UA batch node read failed endpoint=%s batch_size=%d; falling back to per-node reads",
                client._endpoint,
                len(entry_batch),
                exc_info=True,
            )
            for info in await client._read_node_infos_fallback(entry_batch):
                slot_index = index_by_node_id.get(info.node_id)
                if slot_index is not None and slots[slot_index] is None:
                    slots[slot_index] = info
            continue

        object_or_variable_nodes: list[Any] = []
        mandatory_by_node_id: dict[str, tuple[str, str, NodeClass]] = {}
        descriptions_by_node_id: dict[str, str] = {}
        class_attributes_by_node_id: dict[str, dict[AttributeIds, Any]] = {}
        fallback_entries: list[tuple[Any, str | None]] = []

        for index, node in enumerate(nodes):
            node_id = node.nodeid.to_string()
            start = index * len(_NODE_INFO_ATTRIBUTES)
            node_values = {attr: attribute_values[start + offset] for offset, attr in enumerate(_NODE_INFO_ATTRIBUTES)}
            browse_name_value = client._extract_attribute_value(node_values[AttributeIds.BrowseName])
            display_name_value = client._extract_attribute_value(node_values[AttributeIds.DisplayName])
            node_class_value = client._extract_attribute_value(node_values[AttributeIds.NodeClass])
            node_class = client._coerce_node_class(node_class_value)
            if node_class is None:
                fallback_entries.append((node, parent_by_node_id[node_id]))
                continue

            description_value = client._extract_attribute_value(node_values[AttributeIds.Description])
            description = getattr(description_value, "Text", None)
            if isinstance(description, str) and description.strip():
                descriptions_by_node_id[node_id] = description

            if browse_name_value is None or display_name_value is None:
                fallback_entries.append((node, parent_by_node_id[node_id]))
                continue

            browse_name = getattr(browse_name_value, "Name", None)
            display_name = getattr(display_name_value, "Text", None)
            if not isinstance(browse_name, str) or not isinstance(display_name, str):
                fallback_entries.append((node, parent_by_node_id[node_id]))
                continue

            mandatory_by_node_id[node_id] = (browse_name, display_name, node_class)
            applicable_attributes = _NODE_INFO_CLASS_ATTRIBUTES.get(node_class, ())
            class_attributes_by_node_id[node_id] = {
                attr: client._extract_class_node_info_attribute(node_id, node_class, attr, node_values[attr])
                for attr in applicable_attributes
            }
            if node_class in {NodeClass.Object, NodeClass.Variable}:
                object_or_variable_nodes.append(node)

        type_definitions_by_node_id = await client._read_type_definitions(object_or_variable_nodes)

        for node in nodes:
            node_id = node.nodeid.to_string()
            mandatory = mandatory_by_node_id.get(node_id)
            if mandatory is None:
                continue

            browse_name, display_name, node_class = mandatory
            class_attributes = class_attributes_by_node_id[node_id]
            data_type_value = class_attributes.get(AttributeIds.DataType)
            data_type = (
                data_type_value.to_string()
                if data_type_value is not None and hasattr(data_type_value, "to_string")
                else (str(data_type_value) if data_type_value is not None else None)
            )
            executable_value = class_attributes.get(AttributeIds.Executable)
            user_executable_value = class_attributes.get(AttributeIds.UserExecutable)
            slot_index = index_by_node_id.get(node_id)
            if slot_index is None or slots[slot_index] is not None:
                continue
            slots[slot_index] = OpcUaNodeInfo(
                node_id=node_id,
                parent_node_id=parent_by_node_id[node_id],
                browse_name=browse_name,
                display_name=display_name,
                node_class=node_class.name,
                data_type=data_type,
                type_definition_id=_normalize_type_definition_id(type_definitions_by_node_id.get(node_id)),
                event_notifier=bool(class_attributes.get(AttributeIds.EventNotifier, False)),
                description=descriptions_by_node_id.get(node_id),
                access_level=_coerce_access_level(class_attributes.get(AttributeIds.AccessLevel)),
                user_access_level=_coerce_access_level(class_attributes.get(AttributeIds.UserAccessLevel)),
                executable=executable_value if isinstance(executable_value, bool) else None,
                user_executable=user_executable_value if isinstance(user_executable_value, bool) else None,
            )

        if fallback_entries:
            for info in await client._read_node_infos_fallback(fallback_entries):
                slot_index = index_by_node_id.get(info.node_id)
                if slot_index is not None and slots[slot_index] is None:
                    slots[slot_index] = info

    return [item for item in slots if item is not None]


def _extract_class_node_info_attribute(
    client: OpcUaClient,
    node_id: str,
    node_class: NodeClass,
    attribute: AttributeIds,
    data_value: ua.DataValue,
) -> Any | None:
    if attribute not in _NODE_INFO_CLASS_ATTRIBUTES.get(node_class, ()):
        return None
    status_code = data_value.StatusCode
    if status_code is not None and not status_code.is_good():
        logger.debug(
            "OPC UA node attribute unavailable endpoint=%s node_id=%s node_class=%s attribute=%s status=%s",
            client._endpoint,
            node_id,
            node_class.name,
            attribute.name,
            status_code,
        )
        return None
    if data_value.Value is None:
        return None
    return data_value.Value.Value


async def _read_node_infos_fallback(client: OpcUaClient, entries: list[tuple[Any, str | None]]) -> list[OpcUaNodeInfo]:
    semaphore = client._read_slot()

    async def worker(node: Any, parent_node_id: str | None) -> OpcUaNodeInfo | None:
        async with semaphore:
            try:
                return await client._read_node_info(node=node, parent_node_id=parent_node_id)
            except Exception:
                logger.warning(
                    "OPC UA node read failed endpoint=%s node_id=%s; skipping node",
                    client._endpoint,
                    node.nodeid.to_string(),
                    exc_info=True,
                )
                return None

    raw = await _gather_cancelled_on_failure([worker(node, parent_node_id) for node, parent_node_id in entries])
    return [item for item in raw if item is not None]


async def _read_attribute_batch(client: OpcUaClient, nodes: list[Any], attr: AttributeIds) -> list[ua.DataValue]:
    if not nodes:
        return []

    client._runtime_metrics.read_calls += 1
    client._runtime_metrics.read_nodes += len(nodes)
    try:
        values = await client._client.read_attributes(nodes, attr=attr)
        client._record_read_data_values(values)
        return values
    except Exception as exc:
        if not client._should_retry_after_disconnect(exc):
            client._record_failed_request()
            raise
        logger.warning(
            "OPC UA batch attribute read retry after reconnect endpoint=%s attr=%s batch_size=%d",
            client._endpoint,
            int(attr),
            len(nodes),
        )
        await client._wait_for_reconnect(exc)
        retry_nodes = [client._client.get_node(node.nodeid) for node in nodes]
        values = await client._retry_call(client._client.read_attributes(retry_nodes, attr=attr))
        client._record_read_data_values(values)
        return values


async def _read_attribute_pairs(
    client: OpcUaClient,
    node_attributes: list[tuple[Any, AttributeIds]],
    batch_size: int | None = None,
) -> list[ua.DataValue]:
    if not node_attributes:
        return []

    if batch_size is None:
        limits = await client.get_operational_limits()
        batch_size = limits.max_nodes_per_read or len(node_attributes)

    batches = _chunked_nodes(node_attributes, max(1, batch_size))
    semaphore = client._read_slot()

    async def read_batch(batch: list[tuple[Any, AttributeIds]]) -> list[ua.DataValue]:
        async with semaphore:
            return await client._read_attribute_pairs_batch(batch)

    batch_values = await _gather_cancelled_on_failure([read_batch(batch) for batch in batches])
    return [value for values in batch_values for value in values]


async def _read_attribute_pairs_batch(
    client: OpcUaClient,
    node_attributes: list[tuple[Any, AttributeIds]],
) -> list[ua.DataValue]:
    params = ua.ReadParameters()
    params.MaxAge = 0
    params.TimestampsToReturn = ua.TimestampsToReturn.Both
    params.NodesToRead = [ua.ReadValueId(NodeId=node.nodeid, AttributeId=int(attr)) for node, attr in node_attributes]

    client._runtime_metrics.read_calls += 1
    client._runtime_metrics.read_nodes += len(node_attributes)
    try:
        values = await client._client.uaclient.read(params)
    except Exception as exc:
        if client._should_retry_after_disconnect(exc):
            logger.warning(
                "OPC UA multi-attribute read retry after reconnect endpoint=%s batch_size=%d",
                client._endpoint,
                len(node_attributes),
            )
            await client._wait_for_reconnect(exc)
            values = await client._retry_call(client._client.uaclient.read(params))
        elif client._is_too_many_operations_error(exc) and len(node_attributes) > 1:
            split_at = max(1, len(node_attributes) // 2)
            logger.warning(
                "OPC UA multi-attribute read split retry endpoint=%s batch_size=%d split_at=%d",
                client._endpoint,
                len(node_attributes),
                split_at,
            )
            left, right = await asyncio.gather(
                client._read_attribute_pairs_batch(node_attributes[:split_at]),
                client._read_attribute_pairs_batch(node_attributes[split_at:]),
            )
            return left + right
        else:
            client._record_failed_request()
            raise

    if len(values) != len(node_attributes):
        client._record_failed_request()
        raise RuntimeError(f"Read returned {len(values)} results for {len(node_attributes)} attributes")
    client._record_read_data_values(values)
    return values


async def _read_attribute_batch_limited(
    client: OpcUaClient,
    nodes: list[Any],
    attr: AttributeIds,
    batch_size: int | None = None,
) -> list[ua.DataValue]:
    if not nodes:
        return []

    effective_batch_size = batch_size
    if effective_batch_size is None:
        limits = await client.get_operational_limits()
        max_nodes = limits.max_nodes_per_read or len(nodes)
        effective_batch_size = max(1, min(max_nodes, len(nodes)))

    batches = _chunked_nodes(nodes, max(1, effective_batch_size))
    semaphore = client._read_slot()

    async def read_batch(node_batch: list[Any]) -> list[ua.DataValue]:
        async with semaphore:
            try:
                return await client._read_attribute_batch(node_batch, attr)
            except Exception as exc:
                if not client._is_too_many_operations_error(exc) or len(node_batch) <= 1:
                    raise

                reduced_batch_size = max(1, len(node_batch) // 2)
                logger.warning(
                    "OPC UA attribute read split retry endpoint=%s attr=%s batch_size=%d reduced_batch_size=%d",
                    client._endpoint,
                    int(attr),
                    len(node_batch),
                    reduced_batch_size,
                )
                return await client._read_attribute_batch_limited(
                    node_batch,
                    attr,
                    batch_size=reduced_batch_size,
                )

    batch_values = await _gather_cancelled_on_failure([read_batch(node_batch) for node_batch in batches])
    return [value for values_batch in batch_values for value in values_batch]


async def _read_optional_node_ids(
    client: OpcUaClient,
    nodes: list[Any],
    attr: AttributeIds,
    fallback_attr: AttributeIds | None = None,
) -> dict[str, str | None]:
    if not nodes:
        return {}

    values = await client._read_attribute_batch(nodes, attr)
    output: dict[str, str | None] = {}
    fallback_nodes: list[Any] = []

    for node, value in zip(nodes, values, strict=True):
        attribute_value = client._extract_attribute_value(value)
        node_id = node.nodeid.to_string()
        if attribute_value is None:
            if fallback_attr is not None:
                fallback_nodes.append(node)
            continue

        if hasattr(attribute_value, "to_string"):
            output[node_id] = attribute_value.to_string()
        else:
            output[node_id] = str(attribute_value)

    if fallback_attr is not None and fallback_nodes:
        fallback_values = await client._read_attribute_batch(fallback_nodes, fallback_attr)
        for node, value in zip(fallback_nodes, fallback_values, strict=True):
            attribute_value = client._extract_attribute_value(value)
            if attribute_value is None:
                continue
            if hasattr(attribute_value, "to_string"):
                output[node.nodeid.to_string()] = attribute_value.to_string()
            else:
                output[node.nodeid.to_string()] = str(attribute_value)

    return output


async def _read_optional_scalars(client: OpcUaClient, nodes: list[Any], attr: AttributeIds) -> dict[str, Any]:
    if not nodes:
        return {}

    values = await client._read_attribute_batch(nodes, attr)
    output: dict[str, Any] = {}
    for node, value in zip(nodes, values, strict=True):
        attribute_value = client._extract_attribute_value(value)
        if attribute_value is None:
            continue
        output[node.nodeid.to_string()] = attribute_value
    return output


async def _read_type_definitions(client: OpcUaClient, nodes: list[Any]) -> dict[str, str | None]:
    if not nodes:
        return {}

    limits = await client.get_operational_limits()
    max_nodes_per_browse = limits.max_nodes_per_browse or len(nodes)
    browsed = await client._browse_references_descriptions(
        nodes,
        max_nodes_per_browse=max_nodes_per_browse,
        reference_type_id=ObjectIds.HasTypeDefinition,
    )

    output: dict[str, str | None] = {}
    for node, refs in browsed:
        if not refs:
            continue
        output[node.nodeid.to_string()] = _normalize_type_definition_id(refs[0].NodeId.to_string())
    return output
