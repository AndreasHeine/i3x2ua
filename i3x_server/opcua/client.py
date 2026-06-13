from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol

from asyncua import ua
from asyncua.client.client import Client
from asyncua.ua import NodeClass
from asyncua.ua.attribute_ids import AttributeIds
from asyncua.ua.object_ids import ObjectIds

logger = logging.getLogger(__name__)


def _normalize_type_definition_id(type_definition_id: str | None) -> str | None:
    if not type_definition_id:
        return None
    normalized = type_definition_id.strip()
    if normalized in {"i=0", "ns=0;i=0", "nsu=http://opcfoundation.org/UA/;i=0"}:
        return None
    return normalized


@dataclass(slots=True)
class OpcUaNodeInfo:
    node_id: str
    parent_node_id: str | None
    browse_name: str
    display_name: str
    node_class: str
    data_type: str | None
    type_definition_id: str | None = None
    event_notifier: bool = False
    outgoing_references: list[OpcUaReferenceInfo] = field(default_factory=list)


@dataclass(slots=True)
class OpcUaReferenceInfo:
    target_node_id: str
    reference_type_id: str
    reference_browse_name: str


@dataclass(slots=True)
class OpcUaObjectTypeInfo:
    node_id: str
    parent_node_id: str | None
    browse_name: str
    display_name: str
    properties: dict[str, str | None]
    description: str | None = None
    is_abstract: bool | None = None
    members: list[OpcUaObjectTypeMemberInfo] = field(default_factory=list)


@dataclass(slots=True)
class OpcUaObjectTypeMemberInfo:
    node_id: str
    browse_name: str
    display_name: str
    description: str | None
    node_class: str
    data_type: str | None
    modelling_rule: str | None = None
    value: Any = None
    schema_value: Any = None
    variant_type: str | None = None
    is_array: bool | None = None
    value_rank: int | None = None


@dataclass(slots=True)
class OpcUaNamespaceInfo:
    uri: str
    display_name: str


@dataclass(slots=True)
class OpcUaOperationalLimits:
    max_nodes_per_browse: int | None
    max_nodes_per_read: int | None


@dataclass(slots=True)
class OpcUaSubscriptionCapabilities:
    max_monitored_items_per_call: int | None
    max_subscriptions: int | None
    max_monitored_items: int | None
    max_subscriptions_per_session: int | None
    max_monitored_items_per_subscription: int | None


@dataclass(slots=True)
class OpcUaRuntimeMetrics:
    browse_calls: int = 0
    browse_nodes: int = 0
    browse_initial_references: int = 0
    browse_next_calls: int = 0
    browse_next_references: int = 0
    read_calls: int = 0
    read_nodes: int = 0
    history_read_calls: int = 0
    history_read_nodes: int = 0
    namespace_reads: int = 0
    namespace_count_last: int = 0
    namespace_info_builds: int = 0
    namespace_info_count_last: int = 0
    object_type_reads: int = 0
    object_type_count_last: int = 0
    browse_tree_calls: int = 0
    browse_tree_nodes_last: int = 0
    method_calls: int = 0


@dataclass(slots=True)
class OpcUaRequestMetrics:
    read_count: int = 0
    write_count: int = 0
    browse_count: int = 0
    method_call_count: int = 0
    history_read_count: int = 0
    history_write_count: int = 0
    failed_request_count: int = 0
    goodish_qualities: list[str] = field(default_factory=lambda: ["Good", "Uncertain"])


@dataclass(slots=True)
class OpcUaConnectionSnapshot:
    state: str
    endpoint: str
    since: datetime


class OpcUaClientProtocol(Protocol):
    async def browse_tree(self) -> list[OpcUaNodeInfo]: ...

    async def get_namespaces(self) -> list[str]: ...

    async def get_namespace_infos(self) -> list[OpcUaNamespaceInfo]: ...

    async def get_object_types(self) -> list[OpcUaObjectTypeInfo]: ...

    async def get_operational_limits(self) -> OpcUaOperationalLimits: ...

    async def get_subscription_capabilities(self) -> OpcUaSubscriptionCapabilities: ...

    def get_connection_snapshot(self) -> OpcUaConnectionSnapshot: ...

    def snapshot_request_metrics(self) -> OpcUaRequestMetrics: ...

    async def read_value(self, node_id: str) -> Any: ...

    async def read_browse_name(self, node_id: str) -> str | None: ...

    async def read_values(self, node_ids: list[str]) -> list[Any]: ...

    async def read_data_values(self, node_ids: list[str]) -> list[ua.DataValue]: ...

    async def read_server_status_data_value(self) -> ua.DataValue: ...

    async def read_history_values(
        self,
        node_ids: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> dict[str, list[ua.DataValue]]: ...

    async def call_method(self, object_node_id: str, method_node_id: str, args: list[Any]) -> Any: ...

    async def create_datachange_subscription(self, publishing_interval_ms: float, handler: Any) -> Any: ...

    async def subscribe_data_changes(self, subscription: Any, node_ids: list[str]) -> Any: ...

    async def delete_subscription(self, subscription: Any) -> None: ...

    def add_reconnect_listener(self, listener: Callable[[], Awaitable[None]]) -> None: ...


class OpcUaClient:
    def __init__(
        self,
        endpoint: str,
        username: str | None = None,
        password: str | None = None,
        security_mode: str = "None",
        security_policy: str | None = None,
        client_cert_path: str | None = None,
        client_key_path: str | None = None,
        client_key_password: str | None = None,
        server_cert_path: str | None = None,
        browse_concurrency: int = 16,
        metadata_cache_ttl_seconds: int = 300,
    ) -> None:
        self._endpoint = endpoint
        self._username = username.strip() if isinstance(username, str) and username.strip() else None
        self._password = password if isinstance(password, str) and password != "" else None
        self._security_mode = security_mode.strip() if security_mode.strip() else "None"
        self._security_policy = (
            security_policy.strip() if isinstance(security_policy, str) and security_policy.strip() else None
        )
        self._client_cert_path = (
            client_cert_path.strip() if isinstance(client_cert_path, str) and client_cert_path.strip() else None
        )
        self._client_key_path = (
            client_key_path.strip() if isinstance(client_key_path, str) and client_key_path.strip() else None
        )
        self._client_key_password = client_key_password if client_key_password else None
        self._server_cert_path = (
            server_cert_path.strip() if isinstance(server_cert_path, str) and server_cert_path.strip() else None
        )
        self._browse_concurrency = max(1, browse_concurrency)
        self._metadata_cache_ttl_seconds = max(0, metadata_cache_ttl_seconds)
        self._client = Client(url=endpoint)
        self._using_user_auth = False
        self._using_security = False
        if self._username is not None and self._password is not None:
            self._client.set_user(self._username)
            self._client.set_password(self._password)
            self._using_user_auth = True
        elif self._username is not None or self._password is not None:
            logger.warning(
                "OPC UA auth config incomplete endpoint=%s; both username and password are "
                "required. Falling back to anonymous session.",
                self._endpoint,
            )
        self._reconnect_lock = asyncio.Lock()
        self._reconnect_listeners: list[Callable[[], Awaitable[None]]] = []
        self._limits_cache: OpcUaOperationalLimits | None = None
        self._subscription_caps_cache: OpcUaSubscriptionCapabilities | None = None
        self._namespace_infos_cache: tuple[float, list[OpcUaNamespaceInfo]] | None = None
        self._object_types_cache: tuple[float, list[OpcUaObjectTypeInfo]] | None = None
        self._reference_type_supertypes_cache: dict[str, list[str]] = {}
        self._runtime_metrics = OpcUaRuntimeMetrics()
        self._request_metrics = OpcUaRequestMetrics()
        self._goodish_quality_labels = {"good", "uncertain"}
        self._connection_state = "Disconnected"
        self._connection_state_since = datetime.now(tz=timezone.utc)

    def reset_runtime_metrics(self) -> None:
        self._runtime_metrics = OpcUaRuntimeMetrics()
        self._request_metrics = OpcUaRequestMetrics()

    def snapshot_runtime_metrics(self) -> OpcUaRuntimeMetrics:
        return OpcUaRuntimeMetrics(**asdict(self._runtime_metrics))

    def snapshot_request_metrics(self) -> OpcUaRequestMetrics:
        return OpcUaRequestMetrics(**asdict(self._request_metrics))

    def get_connection_snapshot(self) -> OpcUaConnectionSnapshot:
        return OpcUaConnectionSnapshot(
            state=self._connection_state,
            endpoint=self._endpoint,
            since=self._connection_state_since,
        )

    async def resolve_reference_type_supertype_browse_names(self, reference_type_id: str) -> list[str]:
        if not isinstance(reference_type_id, str) or not reference_type_id:
            return []

        cached = self._reference_type_supertypes_cache.get(reference_type_id)
        if cached is not None:
            return list(cached)

        discovered_names: list[str] = []
        seen_names: set[str] = set()
        visited_type_ids: set[str] = set()
        pending_type_ids: list[str] = [reference_type_id]

        while pending_type_ids:
            current_type_id = pending_type_ids.pop()
            if current_type_id in visited_type_ids:
                continue
            visited_type_ids.add(current_type_id)

            type_node = self._client.get_node(current_type_id)
            try:
                browse_name_obj = await type_node.read_browse_name()
                browse_name = getattr(browse_name_obj, "Name", None)
                if isinstance(browse_name, str) and browse_name and browse_name not in seen_names:
                    seen_names.add(browse_name)
                    discovered_names.append(browse_name)
            except Exception:
                logger.debug(
                    "OPC UA browse name read failed for reference type endpoint=%s reference_type_id=%s",
                    self._endpoint,
                    current_type_id,
                    exc_info=True,
                )

            try:
                supertype_refs_by_node = await self._browse_references_descriptions(
                    [type_node],
                    max_nodes_per_browse=1,
                    reference_type_id=ObjectIds.HasSubtype,
                    browse_direction=ua.BrowseDirection.Inverse,
                    include_subtypes=False,
                )
            except Exception:
                logger.debug(
                    "OPC UA supertype browse failed endpoint=%s reference_type_id=%s",
                    self._endpoint,
                    current_type_id,
                    exc_info=True,
                )
                continue

            for _, refs in supertype_refs_by_node:
                for ref in refs:
                    supertype_id = ref.NodeId.to_string()
                    if isinstance(supertype_id, str) and supertype_id and supertype_id not in visited_type_ids:
                        pending_type_ids.append(supertype_id)

        self._reference_type_supertypes_cache[reference_type_id] = list(discovered_names)
        return list(discovered_names)

    async def connect(self) -> None:
        started = perf_counter()
        security_started = perf_counter()
        await self._configure_security_if_needed()
        security_duration_s = perf_counter() - security_started
        logger.info(
            "OPC UA connect started endpoint=%s auth_mode=%s security_mode=%s",
            self._endpoint,
            "userpass" if self._using_user_auth else "anonymous",
            self._security_mode,
        )
        session_started = perf_counter()
        await self._client.connect(
            auto_reconnect=True,
            reconnect_max_delay=30.0,
        )
        self._set_connection_state("Connected")
        session_duration_s = perf_counter() - session_started
        typedef_started = perf_counter()
        await self.load_additional_typedefinitions()
        typedef_duration_s = perf_counter() - typedef_started
        limits_started = perf_counter()
        self._limits_cache = await self.get_operational_limits()
        limits_duration_s = perf_counter() - limits_started
        logger.info(
            "OPC UA limits endpoint=%s max_nodes_per_browse=%s max_nodes_per_read=%s",
            self._endpoint,
            self._limits_cache.max_nodes_per_browse,
            self._limits_cache.max_nodes_per_read,
        )
        logger.info(
            (
                "OPC UA connect phases endpoint=%s security_s=%.3f session_s=%.3f "
                "typedef_s=%.3f limits_s=%.3f total_s=%.3f"
            ),
            self._endpoint,
            security_duration_s,
            session_duration_s,
            typedef_duration_s,
            limits_duration_s,
            perf_counter() - started,
        )

    async def load_additional_typedefinitions(self) -> None:
        started = perf_counter()
        logger.info("OPC UA additional type definitions load started endpoint=%s", self._endpoint)
        try:
            await self._client.load_data_type_definitions()
        except Exception as e:
            logger.warning(
                "OPC UA additional v1.04 data type definitions load failed endpoint=%s error=%s", self._endpoint, e
            )
            try:
                await self._client.load_type_definitions()
            except Exception as e:
                logger.warning(
                    "OPC UA additional v1.03 data type definitions load failed endpoint=%s error=%s", self._endpoint, e
                )
            else:
                logger.info(
                    "OPC UA additional type definitions v1.03 load finished endpoint=%s duration_s=%.3f",
                    self._endpoint,
                    perf_counter() - started,
                )
        else:
            logger.info(
                "OPC UA additional type definitions load finished endpoint=%s duration_s=%.3f",
                self._endpoint,
                perf_counter() - started,
            )

    async def disconnect(self) -> None:
        started = perf_counter()
        logger.info("OPC UA disconnect started endpoint=%s", self._endpoint)
        await self._client.disconnect()
        self._set_connection_state("Disconnected")
        self._limits_cache = None
        self._namespace_infos_cache = None
        self._object_types_cache = None
        self._subscription_caps_cache = None
        logger.info("OPC UA disconnect finished endpoint=%s duration_s=%.3f", self._endpoint, perf_counter() - started)

    async def browse_tree(self) -> list[OpcUaNodeInfo]:
        started = perf_counter()
        self._runtime_metrics.browse_tree_calls += 1
        limits = await self.get_operational_limits()
        max_nodes_per_browse = limits.max_nodes_per_browse or 128
        logger.info(
            "OPC UA browse started endpoint=%s browse_concurrency=%d max_nodes_per_browse=%d",
            self._endpoint,
            self._browse_concurrency,
            max_nodes_per_browse,
        )
        root = self._client.nodes.objects
        output: list[OpcUaNodeInfo] = []
        visited: set[str] = set()
        stack: list[tuple[Any, str | None]] = [(root, None)]

        while stack:
            batch_entries = stack[:max_nodes_per_browse]
            stack = stack[max_nodes_per_browse:]

            filtered_entries: list[tuple[Any, str | None]] = []
            for node, parent_node_id in batch_entries:
                node_id = node.nodeid.to_string()
                if node_id in visited:
                    continue
                visited.add(node_id)
                filtered_entries.append((node, parent_node_id))

            if not filtered_entries:
                continue

            node_infos = await self._read_node_infos_limited(filtered_entries)
            output.extend(node_infos)

            node_info_by_id = {item.node_id: item for item in node_infos}

            all_refs = await self._browse_references_descriptions(
                [node for node, _ in filtered_entries],
                max_nodes_per_browse=max_nodes_per_browse,
                reference_type_id=ObjectIds.References,
            )
            for parent_node, refs in all_refs:
                info = node_info_by_id.get(parent_node.nodeid.to_string())
                if info is None:
                    continue
                info.outgoing_references = self._to_reference_infos(refs)

            nodes = [node for node, _ in filtered_entries]
            browsed = await self._browse_children_descriptions(nodes, max_nodes_per_browse)
            for parent_node, refs in browsed:
                parent_node_id = parent_node.nodeid.to_string()
                for ref in refs:
                    stack.append((self._client.get_node(ref.NodeId), parent_node_id))

        logger.info(
            "OPC UA browse finished endpoint=%s node_count=%d duration_s=%.3f",
            self._endpoint,
            len(output),
            perf_counter() - started,
        )
        self._runtime_metrics.browse_tree_nodes_last = len(output)
        return output

    def _to_reference_infos(self, refs: list[ua.ReferenceDescription]) -> list[OpcUaReferenceInfo]:
        output: list[OpcUaReferenceInfo] = []
        for ref in refs:
            target_node_id = ref.NodeId.to_string()
            reference_type_id = ref.ReferenceTypeId.to_string()
            reference_browse_name = ref.BrowseName.Name
            output.append(
                OpcUaReferenceInfo(
                    target_node_id=target_node_id,
                    reference_type_id=reference_type_id,
                    reference_browse_name=reference_browse_name,
                )
            )
        return output

    async def _read_node_info(self, node: Any, parent_node_id: str | None) -> OpcUaNodeInfo:
        node_id = node.nodeid.to_string()
        browse_name_obj, display_name_obj, node_class_obj = await asyncio.gather(
            node.read_browse_name(),
            node.read_display_name(),
            node.read_node_class(),
        )

        data_type: str | None = None
        type_definition_id: str | None = None
        if node_class_obj == NodeClass.Variable:
            data_type_obj = await node.read_data_type()
            data_type = data_type_obj.to_string()

        if node_class_obj in {NodeClass.Object, NodeClass.Variable}:
            try:
                type_definition_obj = await node.read_type_definition()
                type_definition_id = _normalize_type_definition_id(type_definition_obj.to_string())
            except Exception:
                logger.debug(
                    "OPC UA type definition read failed endpoint=%s node_id=%s",
                    self._endpoint,
                    node_id,
                    exc_info=True,
                )

        event_notifier = bool(await node.read_event_notifier()) if node_class_obj == NodeClass.Object else False

        return OpcUaNodeInfo(
            node_id=node_id,
            parent_node_id=parent_node_id,
            browse_name=browse_name_obj.Name,
            display_name=display_name_obj.Text,
            node_class=node_class_obj.name,
            data_type=data_type,
            type_definition_id=type_definition_id,
            event_notifier=event_notifier,
        )

    async def _read_node_infos_limited(self, entries: list[tuple[Any, str | None]]) -> list[OpcUaNodeInfo]:
        if not entries:
            return []

        limits = await self.get_operational_limits()
        max_nodes = limits.max_nodes_per_read or len(entries)
        batch_size = max(1, min(max_nodes, len(entries)))
        output: list[OpcUaNodeInfo] = []

        for entry_batch in _chunked_nodes(entries, batch_size):
            nodes = [node for node, _ in entry_batch]
            parent_by_node_id = {node.nodeid.to_string(): parent_node_id for node, parent_node_id in entry_batch}

            try:
                browse_names = await self._read_attribute_batch(nodes, AttributeIds.BrowseName)
                display_names = await self._read_attribute_batch(nodes, AttributeIds.DisplayName)
                node_classes = await self._read_attribute_batch(nodes, AttributeIds.NodeClass)
            except Exception:
                logger.warning(
                    "OPC UA batch node read failed endpoint=%s batch_size=%d; falling back to per-node reads",
                    self._endpoint,
                    len(entry_batch),
                    exc_info=True,
                )
                output.extend(await self._read_node_infos_fallback(entry_batch))
                continue

            variable_nodes: list[Any] = []
            object_or_variable_nodes: list[Any] = []
            object_nodes: list[Any] = []
            mandatory_by_node_id: dict[str, tuple[str, str, NodeClass]] = {}
            fallback_entries: list[tuple[Any, str | None]] = []

            for index, node in enumerate(nodes):
                node_id = node.nodeid.to_string()
                browse_name_value = self._extract_attribute_value(browse_names[index])
                display_name_value = self._extract_attribute_value(display_names[index])
                node_class_value = self._extract_attribute_value(node_classes[index])
                node_class = self._coerce_node_class(node_class_value)

                if node_class is None or browse_name_value is None or display_name_value is None:
                    fallback_entries.append((node, parent_by_node_id[node_id]))
                    continue

                browse_name = getattr(browse_name_value, "Name", None)
                display_name = getattr(display_name_value, "Text", None)
                if not isinstance(browse_name, str) or not isinstance(display_name, str):
                    fallback_entries.append((node, parent_by_node_id[node_id]))
                    continue

                mandatory_by_node_id[node_id] = (browse_name, display_name, node_class)
                if node_class == NodeClass.Variable:
                    variable_nodes.append(node)
                    object_or_variable_nodes.append(node)
                elif node_class == NodeClass.Object:
                    object_nodes.append(node)
                    object_or_variable_nodes.append(node)

            data_types_by_node_id = await self._read_optional_node_ids(variable_nodes, AttributeIds.DataType)
            type_definitions_by_node_id = await self._read_type_definitions(object_or_variable_nodes)
            event_notifiers_by_node_id = await self._read_optional_scalars(object_nodes, AttributeIds.EventNotifier)

            for node in nodes:
                node_id = node.nodeid.to_string()
                mandatory = mandatory_by_node_id.get(node_id)
                if mandatory is None:
                    continue

                browse_name, display_name, node_class = mandatory
                output.append(
                    OpcUaNodeInfo(
                        node_id=node_id,
                        parent_node_id=parent_by_node_id[node_id],
                        browse_name=browse_name,
                        display_name=display_name,
                        node_class=node_class.name,
                        data_type=data_types_by_node_id.get(node_id),
                        type_definition_id=_normalize_type_definition_id(type_definitions_by_node_id.get(node_id)),
                        event_notifier=bool(event_notifiers_by_node_id.get(node_id, False)),
                    )
                )

            if fallback_entries:
                output.extend(await self._read_node_infos_fallback(fallback_entries))

        return output

    async def _read_node_infos_fallback(self, entries: list[tuple[Any, str | None]]) -> list[OpcUaNodeInfo]:
        semaphore = asyncio.Semaphore(self._browse_concurrency)

        async def worker(node: Any, parent_node_id: str | None) -> OpcUaNodeInfo | None:
            async with semaphore:
                try:
                    return await self._read_node_info(node=node, parent_node_id=parent_node_id)
                except Exception:
                    logger.warning(
                        "OPC UA node read failed endpoint=%s node_id=%s; skipping node",
                        self._endpoint,
                        node.nodeid.to_string(),
                        exc_info=True,
                    )
                    return None

        raw = await asyncio.gather(*[worker(node, parent_node_id) for node, parent_node_id in entries])
        return [item for item in raw if item is not None]

    async def _read_attribute_batch(self, nodes: list[Any], attr: AttributeIds) -> list[ua.DataValue]:
        if not nodes:
            return []

        self._runtime_metrics.read_calls += 1
        self._runtime_metrics.read_nodes += len(nodes)
        try:
            values = await self._client.read_attributes(nodes, attr=attr)
            self._record_read_data_values(values)
            return values
        except Exception as exc:
            if not self._should_retry_after_disconnect(exc):
                self._record_failed_request()
                raise
            logger.warning(
                "OPC UA batch attribute read retry after reconnect endpoint=%s attr=%s batch_size=%d",
                self._endpoint,
                int(attr),
                len(nodes),
            )
            await self._reconnect()
            retry_nodes = [self._client.get_node(node.nodeid) for node in nodes]
            values = await self._client.read_attributes(retry_nodes, attr=attr)
            self._record_read_data_values(values)
            return values

    async def _read_attribute_batch_limited(
        self,
        nodes: list[Any],
        attr: AttributeIds,
        batch_size: int | None = None,
    ) -> list[ua.DataValue]:
        if not nodes:
            return []

        effective_batch_size = batch_size
        if effective_batch_size is None:
            limits = await self.get_operational_limits()
            max_nodes = limits.max_nodes_per_read or len(nodes)
            effective_batch_size = max(1, min(max_nodes, len(nodes)))

        output: list[ua.DataValue] = []
        for node_batch in _chunked_nodes(nodes, max(1, effective_batch_size)):
            try:
                output.extend(await self._read_attribute_batch(node_batch, attr))
            except Exception as exc:
                if not self._is_too_many_operations_error(exc) or len(node_batch) <= 1:
                    raise

                reduced_batch_size = max(1, len(node_batch) // 2)
                logger.warning(
                    "OPC UA attribute read split retry endpoint=%s attr=%s batch_size=%d reduced_batch_size=%d",
                    self._endpoint,
                    int(attr),
                    len(node_batch),
                    reduced_batch_size,
                )
                output.extend(
                    await self._read_attribute_batch_limited(
                        node_batch,
                        attr,
                        batch_size=reduced_batch_size,
                    )
                )

        return output

    async def _read_optional_node_ids(
        self,
        nodes: list[Any],
        attr: AttributeIds,
        fallback_attr: AttributeIds | None = None,
    ) -> dict[str, str | None]:
        if not nodes:
            return {}

        values = await self._read_attribute_batch(nodes, attr)
        output: dict[str, str | None] = {}
        fallback_nodes: list[Any] = []

        for node, value in zip(nodes, values, strict=True):
            attribute_value = self._extract_attribute_value(value)
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
            fallback_values = await self._read_attribute_batch(fallback_nodes, fallback_attr)
            for node, value in zip(fallback_nodes, fallback_values, strict=True):
                attribute_value = self._extract_attribute_value(value)
                if attribute_value is None:
                    continue
                if hasattr(attribute_value, "to_string"):
                    output[node.nodeid.to_string()] = attribute_value.to_string()
                else:
                    output[node.nodeid.to_string()] = str(attribute_value)

        return output

    async def _read_optional_scalars(self, nodes: list[Any], attr: AttributeIds) -> dict[str, Any]:
        if not nodes:
            return {}

        values = await self._read_attribute_batch(nodes, attr)
        output: dict[str, Any] = {}
        for node, value in zip(nodes, values, strict=True):
            attribute_value = self._extract_attribute_value(value)
            if attribute_value is None:
                continue
            output[node.nodeid.to_string()] = attribute_value
        return output

    async def _read_type_definitions(self, nodes: list[Any]) -> dict[str, str | None]:
        if not nodes:
            return {}

        limits = await self.get_operational_limits()
        max_nodes_per_browse = limits.max_nodes_per_browse or len(nodes)
        browsed = await self._browse_references_descriptions(
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

    def _extract_attribute_value(self, value: ua.DataValue) -> Any | None:
        status_code = value.StatusCode
        if status_code is not None and not status_code.is_good():
            return None
        if value.Value is None:
            return None
        return value.Value.Value

    def _coerce_node_class(self, value: Any) -> NodeClass | None:
        if isinstance(value, NodeClass):
            return value
        if value is None:
            return None
        try:
            return NodeClass(int(value))
        except (TypeError, ValueError):
            return None

    async def get_operational_limits(self) -> OpcUaOperationalLimits:
        if self._limits_cache is not None:
            return self._limits_cache

        started = perf_counter()
        max_nodes_per_browse: int | None = None
        max_nodes_per_read: int | None = None

        try:
            operational_limits_node = self._client.get_node("i=11704")
            children = await operational_limits_node.get_children()
            for child in children:
                browse_name_obj = await child.read_browse_name()
                browse_name = browse_name_obj.Name
                if browse_name not in {"MaxNodesPerBrowse", "MaxNodesPerRead"}:
                    continue

                value = await child.read_value()
                if isinstance(value, int) and value > 0:
                    if browse_name == "MaxNodesPerBrowse":
                        max_nodes_per_browse = value
                    if browse_name == "MaxNodesPerRead":
                        max_nodes_per_read = value
        except Exception:
            logger.warning(
                "OPC UA operational limits read failed endpoint=%s; using internal defaults",
                self._endpoint,
                exc_info=True,
            )

        limits = OpcUaOperationalLimits(
            max_nodes_per_browse=max_nodes_per_browse,
            max_nodes_per_read=max_nodes_per_read,
        )
        self._limits_cache = limits
        logger.info(
            "OPC UA operational limits resolved endpoint=%s "
            "max_nodes_per_browse=%s max_nodes_per_read=%s duration_s=%.3f",
            self._endpoint,
            limits.max_nodes_per_browse,
            limits.max_nodes_per_read,
            perf_counter() - started,
        )
        return limits

    async def get_subscription_capabilities(self) -> OpcUaSubscriptionCapabilities:
        if self._subscription_caps_cache is not None:
            return self._subscription_caps_cache

        started = perf_counter()

        capabilities = OpcUaSubscriptionCapabilities(
            max_monitored_items_per_call=await self._read_positive_int("i=11714"),
            max_subscriptions=await self._read_positive_int("i=24096"),
            max_monitored_items=await self._read_positive_int("i=24097"),
            max_subscriptions_per_session=await self._read_positive_int("i=24098"),
            max_monitored_items_per_subscription=await self._read_positive_int("i=24104"),
        )

        self._subscription_caps_cache = capabilities
        logger.info(
            "OPC UA subscription capabilities resolved endpoint=%s max_items_per_call=%s "
            "max_subscriptions=%s max_monitored_items=%s max_subscriptions_per_session=%s "
            "max_items_per_subscription=%s duration_s=%.3f",
            self._endpoint,
            capabilities.max_monitored_items_per_call,
            capabilities.max_subscriptions,
            capabilities.max_monitored_items,
            capabilities.max_subscriptions_per_session,
            capabilities.max_monitored_items_per_subscription,
            perf_counter() - started,
        )
        return capabilities

    async def get_namespaces(self) -> list[str]:
        started = perf_counter()
        self._runtime_metrics.namespace_reads += 1
        try:
            raw = await self._client.nodes.namespace_array.read_value()
            namespaces = [str(item) for item in raw] if isinstance(raw, list) else []
            self._runtime_metrics.namespace_count_last = len(namespaces)
            logger.info(
                "OPC UA namespaces read ok endpoint=%s count=%d duration_s=%.3f",
                self._endpoint,
                len(namespaces),
                perf_counter() - started,
            )
            return namespaces
        except Exception:
            logger.exception(
                "OPC UA namespaces read failed endpoint=%s duration_s=%.3f",
                self._endpoint,
                perf_counter() - started,
            )
            raise

    async def get_namespace_infos(self) -> list[OpcUaNamespaceInfo]:
        now = perf_counter()
        if self._namespace_infos_cache is not None:
            cached_at, cached_value = self._namespace_infos_cache
            if self._metadata_cache_ttl_seconds == 0 or now - cached_at <= self._metadata_cache_ttl_seconds:
                logger.debug(
                    "OPC UA namespace info cache hit endpoint=%s age_s=%.3f",
                    self._endpoint,
                    now - cached_at,
                )
                return cached_value

        started = perf_counter()
        cold_build = self._runtime_metrics.namespace_info_builds == 0
        uris = await self.get_namespaces()
        display_by_uri: dict[str, str] = {}

        try:
            limits = await self.get_operational_limits()
            max_nodes_per_browse = limits.max_nodes_per_browse or 128
            namespaces_node = self._client.get_node("i=11715")
            namespace_components_browse = await self._browse_children_descriptions(
                [namespaces_node],
                max_nodes_per_browse,
            )
            namespace_components = [
                self._client.get_node(ref.NodeId) for _, refs in namespace_components_browse for ref in refs
            ]
            logger.info(
                "OPC UA namespace metadata components endpoint=%s count=%d",
                self._endpoint,
                len(namespace_components),
            )

            component_display_names = await asyncio.gather(
                *[component.read_display_name() for component in namespace_components]
            )
            component_display_by_id = {
                component.nodeid.to_string(): display_name.Text
                for component, display_name in zip(namespace_components, component_display_names, strict=True)
            }

            component_children_browse = await self._browse_children_descriptions(
                namespace_components,
                max_nodes_per_browse,
            )

            for component, refs in component_children_browse:
                component_display = component_display_by_id.get(component.nodeid.to_string(), "")
                logger.info(
                    "OPC UA namespace metadata component endpoint=%s node_id=%s children=%d",
                    self._endpoint,
                    component.nodeid.to_string(),
                    len(refs),
                )

                for ref in refs:
                    if ref.BrowseName.Name != "NamespaceUri":
                        continue
                    child = self._client.get_node(ref.NodeId)
                    uri_value = await child.read_value()
                    uri = str(uri_value)
                    if uri:
                        display_by_uri[uri] = component_display or uri
                    break
        except Exception:
            logger.warning(
                "OPC UA namespace metadata read from i=11715 failed endpoint=%s; using fallback names",
                self._endpoint,
                exc_info=True,
            )

        infos = [OpcUaNamespaceInfo(uri=uri, display_name=display_by_uri.get(uri, "")) for uri in uris]
        self._namespace_infos_cache = (perf_counter(), infos)
        self._runtime_metrics.namespace_info_builds += 1
        self._runtime_metrics.namespace_info_count_last = len(infos)
        logger.info(
            "OPC UA namespace infos built endpoint=%s count=%d duration_s=%.3f cold_build=%s",
            self._endpoint,
            len(infos),
            perf_counter() - started,
            cold_build,
        )
        return infos

    async def get_object_types(self) -> list[OpcUaObjectTypeInfo]:
        now = perf_counter()
        if self._object_types_cache is not None:
            cached_at, cached_value = self._object_types_cache
            if self._metadata_cache_ttl_seconds == 0 or now - cached_at <= self._metadata_cache_ttl_seconds:
                logger.debug(
                    "OPC UA object types cache hit endpoint=%s age_s=%.3f",
                    self._endpoint,
                    now - cached_at,
                )
                return cached_value

        started = perf_counter()
        limits = await self.get_operational_limits()
        max_nodes_per_browse = limits.max_nodes_per_browse or 128

        root = self._client.nodes.object_types
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
                member_candidates: list[tuple[str, str, str, str, NodeClass, Any]] = []

                browsed = await self._browse_children_descriptions(nodes, max_nodes_per_browse)
                for parent_node, refs in browsed:
                    parent_node_id = parent_node.nodeid.to_string()
                    for ref in refs:
                        child_node_id = ref.NodeId.to_string()
                        if child_node_id in visited:
                            continue
                        if ref.NodeClass == NodeClass.ObjectType:
                            visited.add(child_node_id)
                            child_node = self._client.get_node(ref.NodeId)
                            stack.append((child_node, parent_node_id))

                            browse_name = ref.BrowseName.Name
                            display_name = ref.DisplayName.Text or browse_name
                            discovered[child_node_id] = (parent_node_id, browse_name, display_name, child_node)
                            continue

                        # Collect direct declarations from ObjectTypes for generic schema projection.
                        if parent_node_id != root_node_id and ref.NodeClass in {NodeClass.Variable, NodeClass.Object}:
                            child_node = self._client.get_node(ref.NodeId)
                            property_name = ref.BrowseName.Name or child_node_id
                            display_name = ref.DisplayName.Text or property_name
                            member_candidates.append(
                                (parent_node_id, child_node_id, property_name, display_name, ref.NodeClass, child_node)
                            )

                if member_candidates:
                    member_infos = await self._read_object_type_members_limited(member_candidates)
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

            metadata_by_node_id = await self._read_object_type_metadata([node for _, _, _, node in discovered.values()])

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

            logger.info(
                "OPC UA object types read ok endpoint=%s count=%d duration_s=%.3f",
                self._endpoint,
                len(output),
                perf_counter() - started,
            )
            self._object_types_cache = (perf_counter(), output)
            self._runtime_metrics.object_type_reads += 1
            self._runtime_metrics.object_type_count_last = len(output)
            return output
        except Exception:
            logger.exception(
                "OPC UA object types read failed endpoint=%s duration_s=%.3f",
                self._endpoint,
                perf_counter() - started,
            )
            raise

    async def _read_data_type_or_none(self, node: Any) -> str | None:
        try:
            data_type_obj = await node.read_data_type()
            data_type = data_type_obj.to_string()
            if isinstance(data_type, str):
                return data_type
            return str(data_type)
        except Exception:
            logger.debug(
                "OPC UA variable datatype read failed endpoint=%s node_id=%s",
                self._endpoint,
                node.nodeid.to_string(),
                exc_info=True,
            )
            return None

    async def _read_object_type_members_limited(
        self,
        entries: list[tuple[str, str, str, str, NodeClass, Any]],
    ) -> list[tuple[str, OpcUaObjectTypeMemberInfo]]:
        if not entries:
            return []

        all_nodes = [node for _, _, _, _, _, node in entries]
        all_node_ids = [node.nodeid.to_string() for node in all_nodes]

        description_by_node_id: dict[str, str | None] = {}
        descriptions_batch_ok = True
        try:
            descriptions = await self._read_attribute_batch_limited(all_nodes, AttributeIds.Description)
            for node_id, description_value in zip(all_node_ids, descriptions, strict=True):
                description_raw = self._extract_attribute_value(description_value)
                text_value = getattr(description_raw, "Text", None)
                if isinstance(text_value, str) and text_value.strip():
                    description_by_node_id[node_id] = text_value
                else:
                    description_by_node_id[node_id] = None
        except Exception:
            descriptions_batch_ok = False
            logger.debug(
                "OPC UA member description batch read failed endpoint=%s",
                self._endpoint,
                exc_info=True,
            )

        variable_nodes = [node for _, _, _, _, node_class, node in entries if node_class == NodeClass.Variable]
        variable_node_ids = [node.nodeid.to_string() for node in variable_nodes]

        data_type_by_node_id: dict[str, str | None] = {}
        data_type_batch_ok = True
        if variable_nodes:
            try:
                data_types = await self._read_attribute_batch_limited(variable_nodes, AttributeIds.DataType)
                for node_id, data_type_value in zip(variable_node_ids, data_types, strict=True):
                    data_type_raw = self._extract_attribute_value(data_type_value)
                    if data_type_raw is None:
                        data_type_by_node_id[node_id] = None
                        continue
                    if hasattr(data_type_raw, "to_string"):
                        data_type_by_node_id[node_id] = data_type_raw.to_string()
                    else:
                        data_type_by_node_id[node_id] = str(data_type_raw)
            except Exception:
                data_type_batch_ok = False
                logger.debug(
                    "OPC UA member datatype batch read failed endpoint=%s",
                    self._endpoint,
                    exc_info=True,
                )

        value_rank_by_node_id: dict[str, int | None] = {}
        value_rank_batch_ok = True
        if variable_nodes:
            try:
                value_ranks = await self._read_attribute_batch_limited(variable_nodes, AttributeIds.ValueRank)
                for node_id, value_rank_value in zip(variable_node_ids, value_ranks, strict=True):
                    value_rank_raw = self._extract_attribute_value(value_rank_value)
                    if value_rank_raw is None:
                        value_rank_by_node_id[node_id] = None
                        continue
                    if isinstance(value_rank_raw, int):
                        value_rank_by_node_id[node_id] = value_rank_raw
                    else:
                        try:
                            value_rank_by_node_id[node_id] = int(value_rank_raw)
                        except (TypeError, ValueError):
                            value_rank_by_node_id[node_id] = None
            except Exception:
                value_rank_batch_ok = False
                logger.debug(
                    "OPC UA member value-rank batch read failed endpoint=%s",
                    self._endpoint,
                    exc_info=True,
                )

        semaphore = asyncio.Semaphore(self._browse_concurrency)

        async def worker(
            parent_id: str,
            node_id: str,
            browse_name: str,
            display_name: str,
            node_class: NodeClass,
            node: Any,
        ) -> tuple[str, OpcUaObjectTypeMemberInfo]:
            async with semaphore:
                data_type: str | None = None
                if node_class == NodeClass.Variable:
                    if data_type_batch_ok:
                        data_type = data_type_by_node_id.get(node_id)
                    else:
                        data_type = await self._read_data_type_or_none(node)

                data_value = await self._read_data_value_or_none(node) if node_class == NodeClass.Variable else None
                value = _variant_value_or_self(data_value)
                variant_type, is_array = _variant_metadata(data_value)
                if node_class == NodeClass.Variable:
                    if value_rank_batch_ok:
                        value_rank = value_rank_by_node_id.get(node_id)
                    else:
                        value_rank = await self._read_value_rank_or_none(node)
                else:
                    value_rank = None
                if value_rank is not None and value_rank >= 1:
                    is_array = True
                modelling_rule = await self._read_modelling_rule_or_none(node)
                if descriptions_batch_ok:
                    description = description_by_node_id.get(node_id)
                else:
                    description = await self._read_description_or_none(node)
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
                        modelling_rule=modelling_rule,
                    ),
                )

        return await asyncio.gather(
            *[
                worker(parent_id, node_id, browse_name, display_name, node_class, node)
                for parent_id, node_id, browse_name, display_name, node_class, node in entries
            ]
        )

    async def _read_modelling_rule_or_none(self, node: Any) -> str | None:
        try:
            browsed = await self._browse_references_descriptions(
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
                self._endpoint,
                node.nodeid.to_string(),
                exc_info=True,
            )
            return None

    async def _read_description_or_none(self, node: Any) -> str | None:
        try:
            localized = await node.read_description()
            text = getattr(localized, "Text", None)
            if isinstance(text, str) and text.strip():
                return text
            return None
        except Exception:
            logger.debug(
                "OPC UA description read failed endpoint=%s node_id=%s",
                self._endpoint,
                node.nodeid.to_string(),
                exc_info=True,
            )
            return None

    async def _read_data_value_or_none(self, node: Any) -> Any:
        try:
            return await node.read_data_value()
        except Exception:
            logger.debug(
                "OPC UA data value read failed endpoint=%s node_id=%s",
                self._endpoint,
                node.nodeid.to_string(),
                exc_info=True,
            )
            return None

    async def _read_value_rank_or_none(self, node: Any) -> int | None:
        try:
            value_rank = await node.read_value_rank()
            if isinstance(value_rank, int):
                return value_rank
            return int(value_rank)
        except Exception:
            logger.debug(
                "OPC UA value rank read failed endpoint=%s node_id=%s",
                self._endpoint,
                node.nodeid.to_string(),
                exc_info=True,
            )
            return None

    async def _read_object_type_metadata(self, nodes: list[Any]) -> dict[str, dict[str, Any]]:
        if not nodes:
            return {}

        descriptions = await self._read_attribute_batch(nodes, AttributeIds.Description)
        abstract_flags = await self._read_attribute_batch(nodes, AttributeIds.IsAbstract)

        output: dict[str, dict[str, Any]] = {}
        for node, description_value, abstract_value in zip(nodes, descriptions, abstract_flags, strict=True):
            node_id = node.nodeid.to_string()
            description_raw = self._extract_attribute_value(description_value)
            abstract_raw = self._extract_attribute_value(abstract_value)

            description: str | None = None
            text_value = getattr(description_raw, "Text", None)
            if isinstance(text_value, str) and text_value.strip():
                description = text_value

            is_abstract: bool | None = None
            if abstract_raw is not None:
                is_abstract = bool(abstract_raw)

            output[node_id] = {"description": description, "is_abstract": is_abstract}

        return output

    async def _browse_children_descriptions(
        self,
        nodes: list[Any],
        max_nodes_per_browse: int,
    ) -> list[tuple[Any, list[ua.ReferenceDescription]]]:
        return await self._browse_references_descriptions(
            nodes,
            max_nodes_per_browse=max_nodes_per_browse,
            reference_type_id=ObjectIds.HierarchicalReferences,
        )

    async def _browse_references_descriptions(
        self,
        nodes: list[Any],
        max_nodes_per_browse: int,
        reference_type_id: int,
        browse_direction: ua.BrowseDirection = ua.BrowseDirection.Forward,
        include_subtypes: bool = True,
    ) -> list[tuple[Any, list[ua.ReferenceDescription]]]:
        if not nodes:
            return []

        results: list[tuple[Any, list[ua.ReferenceDescription]]] = []
        for node_batch in _chunked_nodes(nodes, max(1, max_nodes_per_browse)):
            browse_descriptions: list[ua.BrowseDescription] = []
            for node in node_batch:
                desc = ua.BrowseDescription()
                desc.NodeId = node.nodeid
                desc.BrowseDirection = browse_direction
                desc.ReferenceTypeId = ua.NodeId(ua.Int32(reference_type_id))
                desc.IncludeSubtypes = include_subtypes
                desc.NodeClassMask = ua.NodeClass.Unspecified
                desc.ResultMask = ua.BrowseResultMask.All
                browse_descriptions.append(desc)

            params = ua.BrowseParameters()
            params.View = ua.ViewDescription()
            params.RequestedMaxReferencesPerNode = 0
            params.NodesToBrowse = browse_descriptions

            self._runtime_metrics.browse_calls += 1
            self._runtime_metrics.browse_nodes += len(node_batch)
            logger.debug(
                "OPC UA browse batch endpoint=%s nodes_to_browse=%d",
                self._endpoint,
                len(node_batch),
            )
            browse_results = await self._browse_with_retry(params=params, batch_size=len(node_batch))
            initial_references_total = sum(len(result.References) for result in browse_results)
            self._runtime_metrics.browse_initial_references += initial_references_total
            logger.debug(
                "OPC UA browse batch result endpoint=%s nodes=%d initial_references=%d",
                self._endpoint,
                len(node_batch),
                initial_references_total,
            )

            for node, browse_result in zip(node_batch, browse_results, strict=True):
                refs = list(browse_result.References)
                continuation_point = browse_result.ContinuationPoint
                browse_next_calls = 0
                browse_next_references_total = 0

                while continuation_point:
                    next_params = ua.BrowseNextParameters()
                    next_params.ReleaseContinuationPoints = False
                    next_params.ContinuationPoints = [continuation_point]
                    next_results = await self._browse_next_with_retry(
                        next_params=next_params,
                        node_id=node.nodeid.to_string(),
                    )
                    browse_next_calls += 1
                    self._runtime_metrics.browse_next_calls += 1
                    if not next_results:
                        break
                    next_result = next_results[0]
                    refs.extend(next_result.References)
                    browse_next_references_total += len(next_result.References)
                    self._runtime_metrics.browse_next_references += len(next_result.References)
                    continuation_point = next_result.ContinuationPoint

                if browse_next_calls > 0:
                    logger.debug(
                        "OPC UA browse-next endpoint=%s node_id=%s calls=%d "
                        "additional_references=%d total_references=%d",
                        self._endpoint,
                        node.nodeid.to_string(),
                        browse_next_calls,
                        browse_next_references_total,
                        len(refs),
                    )

                results.append((node, refs))

        return results

    async def _browse_with_retry(self, params: ua.BrowseParameters, batch_size: int) -> list[ua.BrowseResult]:
        try:
            results = await self._client.uaclient.browse(params)
            self._record_browse_results(results)
            return results
        except Exception as exc:
            if not self._should_retry_after_disconnect(exc):
                self._record_failed_request()
                raise
            logger.warning(
                "OPC UA browse retry after reconnect endpoint=%s batch_size=%d",
                self._endpoint,
                batch_size,
            )
            await self._reconnect()
            results = await self._client.uaclient.browse(params)
            self._record_browse_results(results)
            return results

    async def _browse_next_with_retry(
        self,
        next_params: ua.BrowseNextParameters,
        node_id: str,
    ) -> list[ua.BrowseResult]:
        try:
            results = await self._client.uaclient.browse_next(next_params)
            self._record_browse_results(results)
            return results
        except Exception as exc:
            if not self._should_retry_after_disconnect(exc):
                self._record_failed_request()
                raise
            logger.warning(
                "OPC UA browse-next retry after reconnect endpoint=%s node_id=%s",
                self._endpoint,
                node_id,
            )
            await self._reconnect()
            results = await self._client.uaclient.browse_next(next_params)
            self._record_browse_results(results)
            return results

    async def read_value(self, node_id: str) -> Any:
        started = perf_counter()
        self._runtime_metrics.read_calls += 1
        self._runtime_metrics.read_nodes += 1
        node = self._client.get_node(node_id)
        try:
            value = await node.read_value()
            self._record_read_success()
            logger.debug("OPC UA read ok node_id=%s duration_s=%.3f", node_id, perf_counter() - started)
            return value
        except Exception as exc:
            if self._should_retry_after_disconnect(exc):
                logger.warning(
                    "OPC UA read retry after reconnect node_id=%s endpoint=%s",
                    node_id,
                    self._endpoint,
                )
                await self._reconnect()
                value = await self._client.get_node(node_id).read_value()
                self._record_read_success()
                logger.debug(
                    "OPC UA read ok after reconnect node_id=%s duration_s=%.3f",
                    node_id,
                    perf_counter() - started,
                )
                return value
            self._record_failed_request()
            logger.exception("OPC UA read failed node_id=%s duration_s=%.3f", node_id, perf_counter() - started)
            raise

    async def read_browse_name(self, node_id: str) -> str | None:
        started = perf_counter()
        node = self._client.get_node(node_id)
        try:
            browse_name = await node.read_browse_name()
            resolved = getattr(browse_name, "Name", None)
            if isinstance(resolved, str) and resolved:
                return resolved
            return str(browse_name) if browse_name is not None else None
        except Exception as exc:
            if self._should_retry_after_disconnect(exc):
                logger.warning(
                    "OPC UA browse-name retry after reconnect node_id=%s endpoint=%s",
                    node_id,
                    self._endpoint,
                )
                await self._reconnect()
                retry_name = await self._client.get_node(node_id).read_browse_name()
                resolved_retry = getattr(retry_name, "Name", None)
                if isinstance(resolved_retry, str) and resolved_retry:
                    return resolved_retry
                return str(retry_name) if retry_name is not None else None
            logger.debug(
                "OPC UA browse-name read failed node_id=%s duration_s=%.3f",
                node_id,
                perf_counter() - started,
                exc_info=True,
            )
            return None

    async def read_values(self, node_ids: list[str]) -> list[Any]:
        if not node_ids:
            return []

        started = perf_counter()
        limits = await self.get_operational_limits()
        max_nodes = limits.max_nodes_per_read or len(node_ids)
        batch_size = max(1, min(max_nodes, len(node_ids)))

        values: list[Any] = []
        for batch in _chunked(node_ids, batch_size):
            values.extend(await self._read_values_batch_with_fallback(batch))

        logger.info(
            "OPC UA batch read ok endpoint=%s requested=%d batch_size=%d duration_s=%.3f",
            self._endpoint,
            len(node_ids),
            batch_size,
            perf_counter() - started,
        )
        return values

    async def read_data_values(self, node_ids: list[str]) -> list[ua.DataValue]:
        if not node_ids:
            return []

        started = perf_counter()
        limits = await self.get_operational_limits()
        max_nodes = limits.max_nodes_per_read or len(node_ids)
        batch_size = max(1, min(max_nodes, len(node_ids)))
        semaphore = asyncio.Semaphore(min(batch_size, self._browse_concurrency))

        async def _read_one(node_id: str) -> ua.DataValue:
            async with semaphore:
                node = self._client.get_node(node_id)
                try:
                    value = await node.read_data_value()
                    self._record_read_data_values([value])
                    return value
                except Exception as exc:
                    if self._should_retry_after_disconnect(exc):
                        logger.warning(
                            "OPC UA data-value read retry after reconnect node_id=%s endpoint=%s",
                            node_id,
                            self._endpoint,
                        )
                        await self._reconnect()
                        try:
                            retry_value = await self._client.get_node(node_id).read_data_value()
                            self._record_read_data_values([retry_value])
                            return retry_value
                        except Exception:
                            pass
                    logger.warning(
                        "OPC UA data-value read failed node_id=%s; returning Bad null",
                        node_id,
                        exc_info=True,
                    )
                    dv = ua.DataValue()
                    dv.StatusCode = ua.StatusCode(ua.UInt32(0x80010000))
                    self._record_read_data_values([dv])
                    return dv

        results: list[ua.DataValue] = []
        for chunk in _chunked(node_ids, batch_size):
            chunk_results = await asyncio.gather(*[_read_one(nid) for nid in chunk])
            results.extend(chunk_results)

        self._runtime_metrics.read_calls += 1
        self._runtime_metrics.read_nodes += len(node_ids)
        logger.info(
            "OPC UA batch data-value read ok endpoint=%s requested=%d duration_s=%.3f",
            self._endpoint,
            len(node_ids),
            perf_counter() - started,
        )
        return results

    async def _read_values_batch_with_fallback(self, node_ids: list[str]) -> list[Any]:
        if not node_ids:
            return []

        self._runtime_metrics.read_calls += 1
        self._runtime_metrics.read_nodes += len(node_ids)
        nodes = [self._client.get_node(node_id) for node_id in node_ids]
        try:
            values = list(await self._client.read_values(nodes))
            self._record_read_success()
            return values
        except Exception as exc:
            candidate_error = exc
            if self._should_retry_after_disconnect(exc):
                logger.warning(
                    "OPC UA batch read retry after reconnect endpoint=%s batch_size=%d",
                    self._endpoint,
                    len(node_ids),
                )
                await self._reconnect()
                retry_nodes = [self._client.get_node(node_id) for node_id in node_ids]
                try:
                    values = list(await self._client.read_values(retry_nodes))
                    self._record_read_success()
                    return values
                except Exception as retry_exc:
                    candidate_error = retry_exc

            if len(node_ids) <= 1:
                self._record_failed_request()
                logger.warning(
                    "OPC UA single-node read failed endpoint=%s node_id=%s error=%s; returning null value",
                    self._endpoint,
                    node_ids[0],
                    candidate_error,
                )
                return [None]

            split_at = max(1, len(node_ids) // 2)
            logger.warning(
                "OPC UA batch read split fallback endpoint=%s batch_size=%d split_at=%d error=%s",
                self._endpoint,
                len(node_ids),
                split_at,
                candidate_error,
            )
            left = await self._read_values_batch_with_fallback(node_ids[:split_at])
            right = await self._read_values_batch_with_fallback(node_ids[split_at:])
            return left + right

    async def read_history_values(
        self,
        node_ids: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> dict[str, list[ua.DataValue]]:
        if not node_ids:
            return {}

        started = perf_counter()
        self._runtime_metrics.history_read_calls += len(node_ids)
        self._runtime_metrics.history_read_nodes += len(node_ids)
        semaphore = asyncio.Semaphore(self._browse_concurrency)

        async def worker(node_id: str) -> tuple[str, list[ua.DataValue]]:
            async with semaphore:
                node = self._client.get_node(node_id)
                try:
                    values = await node.read_raw_history(
                        starttime=start_time,
                        endtime=end_time,
                        numvalues=0,
                        return_bounds=False,
                    )
                    self._record_history_read_success()
                except Exception as exc:
                    if not self._should_retry_after_disconnect(exc):
                        self._record_failed_request()
                        raise
                    logger.warning(
                        "OPC UA history read retry after reconnect endpoint=%s node_id=%s",
                        self._endpoint,
                        node_id,
                    )
                    await self._reconnect()
                    retry_node = self._client.get_node(node_id)
                    values = await retry_node.read_raw_history(
                        starttime=start_time,
                        endtime=end_time,
                        numvalues=0,
                        return_bounds=False,
                    )
                    self._record_history_read_success()
                return node_id, values

        pairs = await asyncio.gather(*[worker(node_id) for node_id in node_ids])
        values_by_node_id = {node_id: values for node_id, values in pairs}

        logger.info(
            "OPC UA history read ok endpoint=%s nodes=%d values=%d duration_s=%.3f",
            self._endpoint,
            len(node_ids),
            sum(len(item) for item in values_by_node_id.values()),
            perf_counter() - started,
        )
        return values_by_node_id

    async def call_method(self, object_node_id: str, method_node_id: str, args: list[Any]) -> Any:
        started = perf_counter()
        self._runtime_metrics.method_calls += 1
        object_node = self._client.get_node(object_node_id)
        try:
            result = await object_node.call_method(method_node_id, *args)
            self._record_method_success()
            logger.info(
                "OPC UA method ok object_node_id=%s method_node_id=%s arg_count=%d duration_s=%.3f",
                object_node_id,
                method_node_id,
                len(args),
                perf_counter() - started,
            )
            return result
        except Exception as exc:
            if self._should_retry_after_disconnect(exc):
                logger.warning(
                    "OPC UA method retry after reconnect object_node_id=%s method_node_id=%s",
                    object_node_id,
                    method_node_id,
                )
                await self._reconnect()
                retry_object = self._client.get_node(object_node_id)
                result = await retry_object.call_method(method_node_id, *args)
                self._record_method_success()
                logger.info(
                    "OPC UA method ok after reconnect object_node_id=%s method_node_id=%s arg_count=%d duration_s=%.3f",
                    object_node_id,
                    method_node_id,
                    len(args),
                    perf_counter() - started,
                )
                return result
            self._record_failed_request()
            logger.exception(
                "OPC UA method failed object_node_id=%s method_node_id=%s arg_count=%d duration_s=%.3f",
                object_node_id,
                method_node_id,
                len(args),
                perf_counter() - started,
            )
            raise

    async def _reconnect(self) -> None:
        listeners: list[Callable[[], Awaitable[None]]]
        async with self._reconnect_lock:
            self._set_connection_state("Reconnecting")
            logger.info("OPC UA reconnect started endpoint=%s", self._endpoint)
            try:
                await self._client.disconnect()
            except Exception:
                logger.debug("OPC UA reconnect disconnect step failed endpoint=%s", self._endpoint, exc_info=True)
            await self._configure_security_if_needed()
            await self._client.connect(
                auto_reconnect=True,
                reconnect_max_delay=30.0,
            )
            await self.load_additional_typedefinitions()
            self._set_connection_state("Connected")
            self._limits_cache = None
            self._subscription_caps_cache = None
            self._namespace_infos_cache = None
            self._object_types_cache = None
            logger.info("OPC UA reconnect finished endpoint=%s", self._endpoint)
            listeners = list(self._reconnect_listeners)

        for listener in listeners:
            try:
                await listener()
            except Exception:
                logger.exception("OPC UA reconnect listener failed endpoint=%s", self._endpoint)

    def _should_retry_after_disconnect(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return "connection is closed" in text or "connection is not open" in text

    def _is_too_many_operations_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return "badtoomanyoperations" in text or "too many operations" in text

    def add_reconnect_listener(self, listener: Callable[[], Awaitable[None]]) -> None:
        self._reconnect_listeners.append(listener)

    async def read_server_status_data_value(self) -> ua.DataValue:
        values = await self.read_data_values(["i=2256"])
        if not values:
            self._record_failed_request()
            raise RuntimeError("OPC UA ServerStatus read returned no data value")
        return values[0]

    async def create_datachange_subscription(self, publishing_interval_ms: float, handler: Any) -> Any:
        return await self._client.create_subscription(publishing_interval_ms, handler)

    async def subscribe_data_changes(self, subscription: Any, node_ids: list[str]) -> Any:
        nodes = [self._client.get_node(node_id) for node_id in node_ids]
        return await subscription.subscribe_data_change(nodes)

    async def delete_subscription(self, subscription: Any) -> None:
        await subscription.delete()

    async def _read_positive_int(self, node_id: str) -> int | None:
        try:
            value = await self._client.get_node(node_id).read_value()
        except Exception:
            logger.debug("OPC UA subscription capability read failed node_id=%s", node_id, exc_info=True)
            return None

        if isinstance(value, int) and value > 0:
            return value
        return None

    async def _configure_security_if_needed(self) -> None:
        mode = self._security_mode.strip()
        if mode.lower() == "none":
            self._using_security = False
            return

        missing: list[str] = []
        if self._security_policy is None:
            missing.append("security_policy")
        if self._client_cert_path is None:
            missing.append("client_cert_path")
        if self._client_key_path is None:
            missing.append("client_key_path")
        if missing:
            raise ValueError(
                f"OPC UA encryption requires policy, client cert, and client key. Missing: {', '.join(missing)}"
            )

        cert_path_str = self._client_cert_path
        key_path_str = self._client_key_path
        if cert_path_str is None or key_path_str is None:
            raise ValueError("OPC UA encryption paths are not configured")

        cert_path = Path(cert_path_str)
        key_path = Path(key_path_str)
        _assert_file_exists(cert_path, "OPC UA client certificate")
        _assert_file_exists(key_path, "OPC UA client key")

        key_value = str(key_path)
        if self._client_key_password:
            key_value = f"{key_value}::{self._client_key_password}"

        security_policy = self._security_policy
        if security_policy is None:
            raise ValueError("OPC UA security policy is not configured")

        security_items: list[str] = [security_policy, mode, str(cert_path), key_value]
        if self._server_cert_path is not None:
            server_cert = Path(self._server_cert_path)
            _assert_file_exists(server_cert, "OPC UA server certificate")
            security_items.append(str(server_cert))

        security_string = ",".join(security_items)
        await self._client.set_security_string(security_string)
        self._using_security = True

    def _set_connection_state(self, state: str) -> None:
        if self._connection_state == state:
            return
        self._connection_state = state
        self._connection_state_since = datetime.now(tz=timezone.utc)

    def _is_goodish_status(self, status_code: Any) -> bool:
        if status_code is None:
            return True
        is_uncertain = getattr(status_code, "is_uncertain", None)
        if callable(is_uncertain):
            try:
                if bool(is_uncertain()):
                    return "uncertain" in self._goodish_quality_labels
            except Exception:
                pass
        is_good = getattr(status_code, "is_good", None)
        if callable(is_good):
            try:
                if bool(is_good()):
                    return "good" in self._goodish_quality_labels
            except Exception:
                pass
        name = str(getattr(status_code, "name", status_code)).strip().lower()
        if not name:
            return False
        if "uncertain" in name:
            return "uncertain" in self._goodish_quality_labels
        if "good" in name:
            return "good" in self._goodish_quality_labels
        return name in self._goodish_quality_labels

    def _record_read_success(self) -> None:
        self._request_metrics.read_count += 1

    def _record_history_read_success(self) -> None:
        self._request_metrics.history_read_count += 1

    def _record_method_success(self) -> None:
        self._request_metrics.method_call_count += 1

    def _record_failed_request(self) -> None:
        self._request_metrics.failed_request_count += 1

    def _record_browse_results(self, results: list[ua.BrowseResult]) -> None:
        if not results:
            self._record_failed_request()
            return
        if all(self._is_goodish_status(getattr(result, "StatusCode", None)) for result in results):
            self._request_metrics.browse_count += 1
            return
        self._record_failed_request()

    def _record_read_data_values(self, values: list[ua.DataValue]) -> None:
        if not values:
            self._record_failed_request()
            return
        if all(self._is_goodish_status(value.StatusCode) for value in values):
            self._record_read_success()
            return
        self._record_failed_request()


def _chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[idx : idx + size] for idx in range(0, len(values), size)]


def _chunked_nodes(values: list[Any], size: int) -> list[list[Any]]:
    return [values[idx : idx + size] for idx in range(0, len(values), size)]


def _assert_file_exists(path: Path, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")


def _to_json_compatible(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
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


def _variant_metadata(data_value: Any) -> tuple[str | None, bool | None]:
    if data_value is None:
        return None, None
    variant = getattr(data_value, "Value", None)
    if variant is None:
        return None, None

    variant_type = getattr(variant, "VariantType", None)
    variant_type_name = None
    if variant_type is not None:
        variant_type_name = str(getattr(variant_type, "name", variant_type))

    is_array = None
    array_type = getattr(variant, "is_array", None)
    if callable(array_type):
        try:
            is_array = bool(array_type())
        except Exception:
            is_array = None
    if is_array is None:
        dims = getattr(variant, "Dimensions", None)
        if dims:
            is_array = True
    if is_array is None:
        candidate_value = getattr(variant, "Value", None)
        if isinstance(candidate_value, (list, tuple)):
            is_array = True

    return variant_type_name, is_array
