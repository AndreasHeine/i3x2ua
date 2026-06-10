from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol

from asyncua import ua
from asyncua.client.client import Client
from asyncua.ua import NodeClass
from asyncua.ua.attribute_ids import AttributeIds
from asyncua.ua.object_ids import ObjectIds

logger = logging.getLogger(__name__)


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


class OpcUaClientProtocol(Protocol):
    async def browse_tree(self) -> list[OpcUaNodeInfo]: ...

    async def get_namespaces(self) -> list[str]: ...

    async def get_namespace_infos(self) -> list[OpcUaNamespaceInfo]: ...

    async def get_object_types(self) -> list[OpcUaObjectTypeInfo]: ...

    async def get_operational_limits(self) -> OpcUaOperationalLimits: ...

    async def get_subscription_capabilities(self) -> OpcUaSubscriptionCapabilities: ...

    async def read_value(self, node_id: str) -> Any: ...

    async def read_values(self, node_ids: list[str]) -> list[Any]: ...

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
        self._runtime_metrics = OpcUaRuntimeMetrics()

    def reset_runtime_metrics(self) -> None:
        self._runtime_metrics = OpcUaRuntimeMetrics()

    def snapshot_runtime_metrics(self) -> OpcUaRuntimeMetrics:
        return OpcUaRuntimeMetrics(**asdict(self._runtime_metrics))

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
                "OPC UA additional type definitions load finished endpoint=%s duration_s=%.3f",
                self._endpoint,
                perf_counter() - started,
            )

    async def disconnect(self) -> None:
        started = perf_counter()
        logger.info("OPC UA disconnect started endpoint=%s", self._endpoint)
        await self._client.disconnect()
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
                type_definition_id = type_definition_obj.to_string()
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
                        type_definition_id=type_definitions_by_node_id.get(node_id),
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
            return await self._client.read_attributes(nodes, attr=attr)
        except Exception as exc:
            if not self._should_retry_after_disconnect(exc):
                raise
            logger.warning(
                "OPC UA batch attribute read retry after reconnect endpoint=%s attr=%s batch_size=%d",
                self._endpoint,
                int(attr),
                len(nodes),
            )
            await self._reconnect()
            retry_nodes = [self._client.get_node(node.nodeid) for node in nodes]
            return await self._client.read_attributes(retry_nodes, attr=attr)

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
            output[node.nodeid.to_string()] = refs[0].NodeId.to_string()
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

    async def _read_data_types_limited(
        self,
        entries: list[tuple[str, str, Any]],
    ) -> list[tuple[str, str, str | None]]:
        semaphore = asyncio.Semaphore(self._browse_concurrency)

        async def worker(parent_id: str, property_name: str, node: Any) -> tuple[str, str, str | None]:
            async with semaphore:
                data_type = await self._read_data_type_or_none(node)
                return (parent_id, property_name, data_type)

        return await asyncio.gather(
            *[worker(parent_id, property_name, node) for parent_id, property_name, node in entries]
        )

    async def _read_object_type_members_limited(
        self,
        entries: list[tuple[str, str, str, str, NodeClass, Any]],
    ) -> list[tuple[str, OpcUaObjectTypeMemberInfo]]:
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
                data_type = await self._read_data_type_or_none(node) if node_class == NodeClass.Variable else None
                value = await self._read_value_or_none(node) if node_class == NodeClass.Variable else None
                modelling_rule = await self._read_modelling_rule_or_none(node)
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

    async def _read_value_or_none(self, node: Any) -> Any:
        try:
            return await node.read_value()
        except Exception:
            logger.debug(
                "OPC UA value read failed endpoint=%s node_id=%s",
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
    ) -> list[tuple[Any, list[ua.ReferenceDescription]]]:
        if not nodes:
            return []

        results: list[tuple[Any, list[ua.ReferenceDescription]]] = []
        for node_batch in _chunked_nodes(nodes, max(1, max_nodes_per_browse)):
            browse_descriptions: list[ua.BrowseDescription] = []
            for node in node_batch:
                desc = ua.BrowseDescription()
                desc.NodeId = node.nodeid
                desc.BrowseDirection = ua.BrowseDirection.Forward
                desc.ReferenceTypeId = ua.NodeId(ua.Int32(reference_type_id))
                desc.IncludeSubtypes = True
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
            browse_results = await self._client.uaclient.browse(params)
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
                    next_results = await self._client.uaclient.browse_next(next_params)
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

    async def read_value(self, node_id: str) -> Any:
        started = perf_counter()
        self._runtime_metrics.read_calls += 1
        self._runtime_metrics.read_nodes += 1
        node = self._client.get_node(node_id)
        try:
            value = await node.read_value()
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
                logger.debug(
                    "OPC UA read ok after reconnect node_id=%s duration_s=%.3f",
                    node_id,
                    perf_counter() - started,
                )
                return value
            logger.exception("OPC UA read failed node_id=%s duration_s=%.3f", node_id, perf_counter() - started)
            raise

    async def read_values(self, node_ids: list[str]) -> list[Any]:
        if not node_ids:
            return []

        started = perf_counter()
        limits = await self.get_operational_limits()
        max_nodes = limits.max_nodes_per_read or len(node_ids)
        batch_size = max(1, min(max_nodes, len(node_ids)))

        values: list[Any] = []
        for batch in _chunked(node_ids, batch_size):
            self._runtime_metrics.read_calls += 1
            self._runtime_metrics.read_nodes += len(batch)
            nodes = [self._client.get_node(node_id) for node_id in batch]
            try:
                batch_values = await self._client.read_values(nodes)
            except Exception as exc:
                if not self._should_retry_after_disconnect(exc):
                    raise
                logger.warning(
                    "OPC UA batch read retry after reconnect endpoint=%s batch_size=%d",
                    self._endpoint,
                    len(batch),
                )
                await self._reconnect()
                retry_nodes = [self._client.get_node(node_id) for node_id in batch]
                batch_values = await self._client.read_values(retry_nodes)
            values.extend(batch_values)

        logger.info(
            "OPC UA batch read ok endpoint=%s requested=%d batch_size=%d duration_s=%.3f",
            self._endpoint,
            len(node_ids),
            batch_size,
            perf_counter() - started,
        )
        return values

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
                except Exception as exc:
                    if not self._should_retry_after_disconnect(exc):
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
                logger.info(
                    "OPC UA method ok after reconnect object_node_id=%s method_node_id=%s arg_count=%d duration_s=%.3f",
                    object_node_id,
                    method_node_id,
                    len(args),
                    perf_counter() - started,
                )
                return result
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
            self._limits_cache = None
            self._subscription_caps_cache = None
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

    def add_reconnect_listener(self, listener: Callable[[], Awaitable[None]]) -> None:
        self._reconnect_listeners.append(listener)

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
    if isinstance(value, (list, tuple)):
        return [_to_json_compatible(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_json_compatible(item) for key, item in value.items()}
    return str(value)
