"""OPC UA adapter: connection lifecycle, metadata caching, metrics and entry points.

The transport internals are split into seam modules that each own one concern:
``recovery.py`` (asyncua supervisor, reconnect hand-off, error classification),
``connection.py`` (connect/disconnect, state mirroring, security setup),
``metadata_cache.py`` (TTL rules, dict caches, coalesced loads), ``metrics.py``
(request/runtime metric recording), ``browse.py`` (tree traversal and
Browse/BrowseNext plumbing), ``node_reading.py`` (node-info attribute batches),
``metadata.py`` (operational limits, subscription capabilities, namespaces),
``object_types.py`` (object-type graph and type hierarchies) and ``values.py``
(reads, writes, history, method calls). Seam functions take the owning client and
call back through its public seams, so monkeypatching a client method still
intercepts the seam; direct asyncua bypasses go through the adapter-owned escape
hatches (``raw_client``, ``browse_raw``, ``browse_next_raw``, ``raw_node``) instead of
private attributes. Internals are tested in the source module that now owns them
(docs/architecture.md).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, TypeVar, cast

from asyncua import ua
from asyncua.client.ua_client import UaClientState
from asyncua.common.node import Node
from asyncua.ua import NodeClass
from asyncua.ua.attribute_ids import AttributeIds

from i3x_server.domain.ports.opcua import (
    DataValueDTO,
    OpcUaClientProtocol,
    OpcUaConnectionSnapshot,
    OpcUaDataChangeHandler,
    OpcUaNamespaceInfo,
    OpcUaNodeInfo,
    OpcUaObjectTypeInfo,
    OpcUaObjectTypeMemberInfo,
    OpcUaOperationalLimits,
    OpcUaReferenceInfo,
    OpcUaRequestMetrics,
    OpcUaRuntimeMetrics,
    OpcUaSubscriptionCapabilities,
    OpcUaSubscriptionHandle,
    OpcUaTypeDefinitionInfo,
    OpcUaWriteMetadataUnsupportedError,
    OpcUaWriteMetadataVerificationError,
    SecurityMode,
)
from i3x_server.infrastructure.opcua import (
    browse,
    connection,
    metadata,
    metadata_cache,
    metrics,
    node_reading,
    object_types,
    recovery,
    values,
)
from i3x_server.infrastructure.opcua.recovery import _SupervisedAsyncuaClient

__all__ = [
    "DataValueDTO",
    "OpcUaClient",
    "OpcUaClientProtocol",
    "model_refresh_budget",
    "OpcUaConnectionSnapshot",
    "OpcUaDataChangeHandler",
    "OpcUaNamespaceInfo",
    "OpcUaNodeInfo",
    "OpcUaObjectTypeInfo",
    "OpcUaObjectTypeMemberInfo",
    "OpcUaOperationalLimits",
    "OpcUaReferenceInfo",
    "OpcUaRequestMetrics",
    "OpcUaRuntimeMetrics",
    "OpcUaSubscriptionCapabilities",
    "OpcUaSubscriptionHandle",
    "OpcUaTypeDefinitionInfo",
    "OpcUaWriteMetadataUnsupportedError",
    "OpcUaWriteMetadataVerificationError",
]

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Budget override for background model refreshes. Tasks created inside the scope inherit
# it through their copied context, so a rebuild's round-trips are throttled by the smaller
# refresh budget instead of competing for the read budget that request handlers share.
_MODEL_REFRESH_BUDGET: ContextVar[asyncio.Semaphore | None] = ContextVar("i3x2ua_opcua_refresh_budget", default=None)


@asynccontextmanager
async def model_refresh_budget(opcua_client: Any) -> AsyncIterator[None]:
    """Throttle OPC UA round-trips made inside the scope by the client's refresh budget.

    Clients without a dedicated refresh budget (test doubles) run unthrottled.
    """
    semaphore = getattr(opcua_client, "_refresh_semaphore", None)
    if semaphore is None:
        yield
        return
    token = _MODEL_REFRESH_BUDGET.set(semaphore)
    try:
        yield
    finally:
        _MODEL_REFRESH_BUDGET.reset(token)


class _NotificationBridge:
    """Adapts asyncua datachange notifications to the domain port callback.

    asyncua hands raw ``Node`` objects and notification structs; the port expects
    the node id as a string and a :class:`DataValueDTO`. A notification whose node
    id cannot be resolved is dropped here (the port callback never sees raw
    transport objects).
    """

    def __init__(self, handler: OpcUaDataChangeHandler) -> None:
        self._handler = handler

    def datachange_notification(self, node: Any, value: Any, data: Any) -> None:
        try:
            node_id = node.nodeid.to_string()
        except Exception:
            logger.warning("Unable to resolve node_id for datachange notification; dropping update")
            return
        monitored_item = getattr(data, "monitored_item", None)
        raw_data_value = getattr(monitored_item, "Value", None)
        self._handler.datachange_notification(
            node_id, value, values.to_data_value_dto(raw_data_value) if raw_data_value is not None else None
        )

    def event_notification(self, event: Any) -> None:
        self._handler.event_notification(event)


class OpcUaClient:
    def __init__(
        self,
        endpoint: str,
        username: str | None = None,
        password: str | None = None,
        security_mode: str = SecurityMode.NONE,
        security_policy: str | None = None,
        client_cert_path: str | None = None,
        client_key_path: str | None = None,
        client_key_password: str | None = None,
        server_cert_path: str | None = None,
        browse_concurrency: int = 16,
        read_concurrency: int = 64,
        refresh_concurrency: int = 32,
        metadata_cache_ttl_seconds: int = 300,
        connection_monitor_interval_seconds: int = 5,
        reconnect_request_timeout_seconds: float = 10.0,
        reconnect_max_delay_seconds: float = 30.0,
        browse_roots: list[str] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self._username = username.strip() if isinstance(username, str) and username.strip() else None
        self._password = password if isinstance(password, str) and password != "" else None
        self.security_mode = security_mode.strip() if security_mode.strip() else SecurityMode.NONE
        self.security_policy = (
            security_policy.strip() if isinstance(security_policy, str) and security_policy.strip() else None
        )
        self.client_cert_path = (
            client_cert_path.strip() if isinstance(client_cert_path, str) and client_cert_path.strip() else None
        )
        self.client_key_path = (
            client_key_path.strip() if isinstance(client_key_path, str) and client_key_path.strip() else None
        )
        self.client_key_password = client_key_password if client_key_password else None
        self.server_cert_path = (
            server_cert_path.strip() if isinstance(server_cert_path, str) and server_cert_path.strip() else None
        )
        self.browse_concurrency = max(1, browse_concurrency)
        # Separate budgets: request handlers fan attribute reads out under the read budget,
        # background refreshes use the smaller refresh budget, and writes keep the
        # transport-level application gate (``application_request_limit`` below).
        self._read_concurrency = max(1, read_concurrency)
        self._refresh_concurrency = max(1, refresh_concurrency)
        self._read_semaphore = asyncio.Semaphore(self._read_concurrency)
        self._refresh_semaphore = asyncio.Semaphore(self._refresh_concurrency)
        self.browse_roots = tuple(browse_roots) if browse_roots is not None else None
        self.metadata_cache_ttl_seconds = max(0, metadata_cache_ttl_seconds)
        self._connection_monitor_interval_seconds = max(1, connection_monitor_interval_seconds)
        self.reconnect_request_timeout_seconds = max(0.1, float(reconnect_request_timeout_seconds))
        self.reconnect_max_delay_seconds = max(1.0, float(reconnect_max_delay_seconds))
        # asyncua's connection supervisor is the single owner of reconnects: it probes the
        # server every watchdog interval, re-activates or recreates the session with backoff,
        # gates new requests while reconnecting, and restores its tracked subscriptions.
        self.raw_client = _SupervisedAsyncuaClient(
            url=endpoint,
            watchdog_intervall=float(self._connection_monitor_interval_seconds),
            reconnect_max_delay=self.reconnect_max_delay_seconds,
            reconnect_request_timeout=self.reconnect_request_timeout_seconds,
            application_request_limit=self.browse_concurrency,
        )
        self.raw_client.reconnected_callback = self.on_asyncua_reconnected
        self.raw_client.application_uri = "urn:i3x2ua:opcua_client"
        self.raw_client.name = "i3x2ua-opcua-client"
        self.raw_client.description = "The i3X API Gateway for OPC UA"
        self.raw_client.product_uri = "urn:github.com/AndreasHeine/i3x2ua.git"
        self.using_user_auth = False
        self.using_security = False
        if self._username is not None and self._password is not None:
            self.raw_client.set_user(self._username)
            self.raw_client.set_password(self._password)
            self.using_user_auth = True
        elif self._username is not None or self._password is not None:
            logger.warning(
                "OPC UA auth config incomplete endpoint=%s; both username and password are "
                "required. Falling back to anonymous session.",
                self.endpoint,
            )
        self.reconnect_listeners: list[Callable[[], Awaitable[None]]] = []
        self.reconnected_event = asyncio.Event()
        self.reconnect_session_recreated = False
        self.limits_cache: OpcUaOperationalLimits | None = None
        self.subscription_caps_cache: OpcUaSubscriptionCapabilities | None = None
        self.namespace_infos_cache: tuple[float, list[OpcUaNamespaceInfo]] | None = None
        self.object_type_cache: tuple[float, list[OpcUaObjectTypeInfo]] | None = None
        self.metadata_load_tasks: dict[str, asyncio.Future[Any]] = {}
        self.stale_metadata_incidents: dict[str, tuple[float, int]] = {}
        self._reference_type_supertypes_cache: dict[str, tuple[float, list[str]]] = {}
        self._type_definition_hierarchy_cache: dict[str, tuple[float, list[OpcUaTypeDefinitionInfo]]] = {}
        self.runtime_metrics = OpcUaRuntimeMetrics()
        self.history_update_support: bool | None = None
        self.request_metrics = OpcUaRequestMetrics()
        self.connection_state = "Disconnected"
        self.connection_state_since = datetime.now(tz=timezone.utc)
        self.connection_state_task: asyncio.Task[None] | None = None
        self.reconnect_handler_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Metrics and connection snapshot
    # ------------------------------------------------------------------

    def reset_runtime_metrics(self) -> None:
        metrics.reset_runtime_metrics(self)

    def snapshot_runtime_metrics(self) -> OpcUaRuntimeMetrics:
        return metrics.snapshot_runtime_metrics(self)

    def snapshot_request_metrics(self) -> OpcUaRequestMetrics:
        return metrics.snapshot_request_metrics(self)

    def get_connection_snapshot(self) -> OpcUaConnectionSnapshot:
        return OpcUaConnectionSnapshot(
            state=self.connection_state,
            endpoint=self.endpoint,
            since=self.connection_state_since,
        )

    def read_slot(self) -> asyncio.Semaphore:
        """Budget for concurrent read round-trips on the current task.

        Request handlers share ``opcua_read_concurrency``; background model refreshes run
        inside :func:`model_refresh_budget` and use the smaller
        ``opcua_refresh_concurrency`` budget, so a rebuild cannot starve readers. Writes
        stay on the transport-level application gate and are unaffected.
        """
        budget = _MODEL_REFRESH_BUDGET.get()
        return self._read_semaphore if budget is None else budget

    def _is_goodish_status(self, status_code: Any) -> bool:
        """Kept as the client-facing entry point; the classification lives in ``metrics``."""
        return metrics.is_goodish_status(status_code)

    def record_read_success(self) -> None:
        metrics.record_read_success(self)

    def record_history_read_success(self) -> None:
        metrics.record_history_read_success(self)

    def record_method_success(self) -> None:
        metrics.record_method_success(self)

    def record_failed_request(self) -> None:
        metrics.record_failed_request(self)

    def record_browse_results(self, results: list[ua.BrowseResult]) -> None:
        metrics.record_browse_results(self, results)

    def record_read_data_values(self, values: list[ua.DataValue]) -> None:
        metrics.record_read_data_values(self, values)

    # ------------------------------------------------------------------
    # Metadata cache entry points (metadata_cache.py)
    # ------------------------------------------------------------------

    def is_metadata_cache_entry_fresh(self, cached_at: float) -> bool:
        """TTL of 0 means entries never expire, unless explicitly invalidated."""
        return metadata_cache.is_metadata_cache_entry_fresh(self, cached_at)

    @staticmethod
    def invalidate_metadata_entry(cached: tuple[float, _T] | None) -> tuple[float, _T] | None:
        """Keep the value servable as stale, but force the next access to refresh it."""
        return metadata_cache.invalidate_metadata_entry(cached)

    def _metadata_dict_cache_read(self, entries: dict[str, tuple[float, _T]], key: str) -> _T | None:
        """Return the fresh value for ``key`` and mark the entry most-recently-used."""
        return metadata_cache._metadata_dict_cache_read(self, entries, key)

    def _metadata_dict_cache_write(
        self, entries: dict[str, tuple[float, _T]], key: str, value: _T, max_entries: int
    ) -> None:
        """Store an entry as most-recently-used and evict least-recently-used ones past the cap."""
        metadata_cache._metadata_dict_cache_write(self, entries, key, value, max_entries)

    def cached_type_definition_hierarchy(self, type_id: str) -> list[OpcUaTypeDefinitionInfo] | None:
        return self._metadata_dict_cache_read(self._type_definition_hierarchy_cache, type_id)

    def store_type_definition_hierarchy(self, type_id: str, hierarchy: list[OpcUaTypeDefinitionInfo]) -> None:
        self._metadata_dict_cache_write(
            self._type_definition_hierarchy_cache,
            type_id,
            hierarchy,
            metadata_cache._TYPE_DEFINITION_HIERARCHY_CACHE_MAX_ENTRIES,
        )

    def cached_reference_type_supertypes(self, type_id: str) -> list[str] | None:
        return self._metadata_dict_cache_read(self._reference_type_supertypes_cache, type_id)

    def store_reference_type_supertypes(self, type_id: str, names: list[str]) -> None:
        self._metadata_dict_cache_write(
            self._reference_type_supertypes_cache,
            type_id,
            names,
            metadata_cache._REFERENCE_TYPE_SUPERTYPES_CACHE_MAX_ENTRIES,
        )

    def reset_metadata_dict_caches(self) -> None:
        """Drop the node-id keyed caches (reconnect or disconnect made their servers stale)."""
        self._reference_type_supertypes_cache = {}
        self._type_definition_hierarchy_cache = {}

    def _start_metadata_load(self, key: str, loader: Callable[[], Awaitable[_T]]) -> asyncio.Future[_T]:
        return metadata_cache._start_metadata_load(self, key, loader)

    async def _coalesce_metadata_load(self, key: str, loader: Callable[[], Awaitable[_T]]) -> _T:
        """Share one in-flight OPC UA metadata load between concurrent callers."""
        return await metadata_cache._coalesce_metadata_load(self, key, loader)

    async def _get_cached_metadata(
        self,
        key: str,
        cached: tuple[float, _T] | None,
        loader: Callable[[], Awaitable[_T]],
    ) -> _T:
        """Serve stale metadata while one shared background load refreshes it."""
        return await metadata_cache._get_cached_metadata(self, key, cached, loader)

    def _finish_metadata_load(self, key: str, task: asyncio.Future[Any]) -> None:
        metadata_cache._finish_metadata_load(self, key, task)

    # ------------------------------------------------------------------
    # Connection lifecycle entry points (connection.py)
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        await connection.connect(self)

    async def load_additional_typedefinitions(self) -> None:
        await connection.load_additional_typedefinitions(self)

    async def disconnect(self) -> None:
        await connection.disconnect(self)

    def start_connection_tasks(self) -> None:
        connection.start_connection_tasks(self)

    async def stop_connection_tasks(self) -> None:
        await connection.stop_connection_tasks(self)

    async def watch_connection_state(self) -> None:
        """Mirror asyncua's transport state into the connection snapshot."""
        await connection.watch_connection_state(self)

    def apply_client_state(self, state: UaClientState) -> None:
        connection.apply_client_state(self, state)

    def on_asyncua_reconnected(self, session_reused: bool) -> None:
        """Called by the asyncua supervisor once the session and its subscriptions are restored."""
        connection.on_asyncua_reconnected(self, session_reused)

    async def reconnect_handler_loop(self) -> None:
        await connection.reconnect_handler_loop(self)

    async def handle_reconnected(self) -> None:
        await connection.handle_reconnected(self)

    def add_reconnect_listener(self, listener: Callable[[], Awaitable[None]]) -> None:
        self.reconnect_listeners.append(listener)

    def remove_reconnect_listener(self, listener: Callable[[], Awaitable[None]]) -> None:
        with suppress(ValueError):
            self.reconnect_listeners.remove(listener)

    async def configure_security_if_needed(self) -> None:
        await connection.configure_security_if_needed(self)

    def set_connection_state(self, state: str) -> None:
        connection.set_connection_state(self, state)

    # ------------------------------------------------------------------
    # Browse seam entry points (browse.py)
    # ------------------------------------------------------------------

    async def browse_tree(self) -> list[OpcUaNodeInfo]:
        return await browse.browse_tree(self)

    async def resolve_browse_roots(self) -> list[Node]:
        return await browse.resolve_browse_roots(self)

    async def browse_children_descriptions(
        self,
        nodes: list[Any],
        max_nodes_per_browse: int,
    ) -> list[tuple[Any, list[ua.ReferenceDescription]]]:
        return await browse.browse_children_descriptions(self, nodes, max_nodes_per_browse)

    async def browse_references_descriptions(
        self,
        nodes: list[Any],
        max_nodes_per_browse: int,
        reference_type_id: int,
        browse_direction: ua.BrowseDirection = ua.BrowseDirection.Forward,
        include_subtypes: bool = True,
    ) -> list[tuple[Any, list[ua.ReferenceDescription]]]:
        return await browse.browse_references_descriptions(
            self,
            nodes,
            max_nodes_per_browse,
            reference_type_id,
            browse_direction=browse_direction,
            include_subtypes=include_subtypes,
        )

    async def browse_with_retry(self, params: ua.BrowseParameters, batch_size: int) -> list[ua.BrowseResult]:
        return await browse.browse_with_retry(self, params, batch_size)

    async def browse_next_with_retry(
        self,
        next_params: ua.BrowseNextParameters,
        node_ids: list[str],
    ) -> list[ua.BrowseResult]:
        return await browse.browse_next_with_retry(self, next_params, node_ids)

    # ------------------------------------------------------------------
    # Explicit asyncua escape hatches (adapter-owned transport plumbing)
    # ------------------------------------------------------------------

    async def browse_raw(self, params: ua.BrowseParameters) -> list[ua.BrowseResult]:
        """One Browse service call on the raw transport, without retry or metrics.

        The single escape hatch for Browse/BrowseNext plumbing: retry, reconnect
        hand-off and metrics recording stay on the client's ``*_with_retry`` seams.
        """
        return await self.raw_client.uaclient.browse(params)

    async def browse_next_raw(self, params: ua.BrowseNextParameters) -> list[ua.BrowseResult]:
        """One BrowseNext service call on the raw transport (see ``browse_raw``)."""
        return await self.raw_client.uaclient.browse_next(params)

    def raw_node(self, node_id: str | ua.NodeId) -> Node:
        """An asyncua ``Node`` for the adapter's transport plumbing.

        Escape hatch for direct attribute/service reads on nodes; API-level access
        goes through the typed read seams instead.
        """
        return self.raw_client.get_node(node_id)

    # ------------------------------------------------------------------
    # Node-reading seam entry points (node_reading.py)
    # ------------------------------------------------------------------

    async def read_reference_type_browse_names(
        self,
        refs: list[ua.ReferenceDescription],
        names: dict[str, str],
        inverse_names: dict[str, str] | None = None,
    ) -> None:
        await node_reading.read_reference_type_browse_names(self, refs, names, inverse_names)

    def to_reference_infos(
        self,
        refs: list[ua.ReferenceDescription],
        reference_type_names: dict[str, str],
        reference_type_inverse_names: dict[str, str] | None = None,
    ) -> list[OpcUaReferenceInfo]:
        return node_reading.to_reference_infos(refs, reference_type_names, reference_type_inverse_names)

    async def read_node_info(self, node: Any, parent_node_id: str | None) -> OpcUaNodeInfo:
        return await node_reading.read_node_info(self, node, parent_node_id)

    async def read_node_infos_limited(self, entries: list[tuple[Any, str | None]]) -> list[OpcUaNodeInfo]:
        return await node_reading.read_node_infos_limited(self, entries)

    def extract_class_node_info_attribute(
        self,
        node_id: str,
        node_class: NodeClass,
        attribute: AttributeIds,
        data_value: ua.DataValue,
    ) -> Any | None:
        return node_reading.extract_class_node_info_attribute(self, node_id, node_class, attribute, data_value)

    async def read_node_infos_fallback(self, entries: list[tuple[Any, str | None]]) -> list[OpcUaNodeInfo]:
        return await node_reading.read_node_infos_fallback(self, entries)

    async def read_attribute_batch(self, nodes: list[Any], attr: AttributeIds) -> list[ua.DataValue]:
        return await node_reading.read_attribute_batch(self, nodes, attr)

    async def read_attribute_pairs(
        self,
        node_attributes: list[tuple[Any, AttributeIds]],
        batch_size: int | None = None,
    ) -> list[ua.DataValue]:
        return await node_reading.read_attribute_pairs(self, node_attributes, batch_size=batch_size)

    async def read_attribute_pairs_batch(
        self,
        node_attributes: list[tuple[Any, AttributeIds]],
    ) -> list[ua.DataValue]:
        return await node_reading.read_attribute_pairs_batch(self, node_attributes)

    async def read_attribute_batch_limited(
        self,
        nodes: list[Any],
        attr: AttributeIds,
        batch_size: int | None = None,
    ) -> list[ua.DataValue]:
        return await node_reading.read_attribute_batch_limited(self, nodes, attr, batch_size=batch_size)

    async def _read_optional_node_ids(
        self,
        nodes: list[Any],
        attr: AttributeIds,
        fallback_attr: AttributeIds | None = None,
    ) -> dict[str, str | None]:
        return await node_reading._read_optional_node_ids(self, nodes, attr, fallback_attr=fallback_attr)

    async def _read_optional_scalars(self, nodes: list[Any], attr: AttributeIds) -> dict[str, Any]:
        return await node_reading._read_optional_scalars(self, nodes, attr)

    async def read_type_definitions(self, nodes: list[Any]) -> dict[str, str | None]:
        return await node_reading.read_type_definitions(self, nodes)

    def extract_attribute_value(self, value: ua.DataValue) -> Any | None:
        return node_reading.extract_attribute_value(value)

    def coerce_node_class(self, value: Any) -> NodeClass | None:
        return node_reading.coerce_node_class(value)

    # ------------------------------------------------------------------
    # Server-metadata seam entry points (metadata.py)
    # ------------------------------------------------------------------

    async def get_operational_limits(self) -> OpcUaOperationalLimits:
        if self.limits_cache is not None:
            return self.limits_cache
        return await self._coalesce_metadata_load("operational_limits", self._load_operational_limits)

    async def _load_operational_limits(self) -> OpcUaOperationalLimits:
        return await metadata._load_operational_limits(self)

    async def get_subscription_capabilities(self) -> OpcUaSubscriptionCapabilities:
        if self.subscription_caps_cache is not None:
            return self.subscription_caps_cache
        return await self._coalesce_metadata_load("subscription_capabilities", self._load_subscription_capabilities)

    async def _load_subscription_capabilities(self) -> OpcUaSubscriptionCapabilities:
        return await metadata._load_subscription_capabilities(self)

    async def read_positive_int(self, node_id: str) -> int | None:
        return await metadata.read_positive_int(self, node_id)

    async def get_namespaces(self) -> list[str]:
        return await metadata._get_namespaces(self)

    async def get_namespace_infos(self) -> list[OpcUaNamespaceInfo]:
        return await self._get_cached_metadata(
            "namespace_infos", self.namespace_infos_cache, self._load_namespace_infos
        )

    async def _load_namespace_infos(self) -> list[OpcUaNamespaceInfo]:
        return await metadata._load_namespace_infos(self)

    # ------------------------------------------------------------------
    # Object-type and type-hierarchy seam entry points (object_types.py)
    # ------------------------------------------------------------------

    async def get_object_types(self) -> list[OpcUaObjectTypeInfo]:
        return await self._get_cached_metadata("object_types", self.object_type_cache, self._load_object_types)

    async def _load_object_types(self) -> list[OpcUaObjectTypeInfo]:
        return await object_types._load_object_types(self)

    async def read_object_type_members_limited(
        self,
        entries: list[tuple[str, str, str, str, NodeClass, Any, str | None, str | None, int | None]],
    ) -> list[tuple[str, OpcUaObjectTypeMemberInfo]]:
        return await object_types.read_object_type_members_limited(self, entries)

    async def read_data_type_or_none(self, node: Any) -> str | None:
        return await object_types.read_data_type_or_none(self, node)

    async def read_type_definition_id_or_none(self, node: Any) -> str | None:
        return await object_types.read_type_definition_id_or_none(self, node)

    async def read_modelling_rule_or_none(self, node: Any) -> str | None:
        return await object_types.read_modelling_rule_or_none(self, node)

    async def read_description_or_none(self, node: Any) -> str | None:
        return await object_types.read_description_or_none(self, node)

    async def read_data_value_or_none(self, node: Any) -> Any:
        return await object_types.read_data_value_or_none(self, node)

    async def read_value_rank_or_none(self, node: Any) -> int | None:
        return await object_types.read_value_rank_or_none(self, node)

    async def read_object_type_metadata(self, nodes: list[Any]) -> dict[str, dict[str, Any]]:
        return await object_types.read_object_type_metadata(self, nodes)

    async def resolve_reference_type_supertype_browse_names(self, reference_type_id: str) -> list[str]:
        return await object_types.resolve_reference_type_supertype_browse_names(self, reference_type_id)

    async def resolve_type_definition_hierarchy(
        self,
        type_definition_id: str,
    ) -> list[OpcUaTypeDefinitionInfo]:
        return await object_types.resolve_type_definition_hierarchy(self, type_definition_id)

    async def resolve_type_definition_hierarchies(
        self,
        type_definition_ids: list[str],
    ) -> dict[str, list[OpcUaTypeDefinitionInfo]]:
        return await object_types.resolve_type_definition_hierarchies(self, type_definition_ids)

    async def resolve_reference_type_chain(self, type_id: str, in_progress: set[str]) -> list[str]:
        return await object_types.resolve_reference_type_chain(self, type_id, in_progress)

    # ------------------------------------------------------------------
    # Value, history and method seam entry points (values.py)
    # ------------------------------------------------------------------

    async def read_value(self, node_id: str) -> Any:
        return await values.read_value(self, node_id)

    async def read_browse_name(self, node_id: str) -> str | None:
        return await values.read_browse_name(self, node_id)

    async def read_values(self, node_ids: list[str]) -> list[Any]:
        return await values.read_values(self, node_ids)

    async def read_data_values(self, node_ids: list[str]) -> list[DataValueDTO]:
        # Boundary conversion: the seam keeps asyncua DataValues (metrics recording and the
        # per-node fallback need status codes structurally); callers get domain DTOs.
        return [values.to_data_value_dto(value) for value in await values.read_data_values(self, node_ids)]

    async def read_data_values_per_node(self, node_ids: list[str]) -> list[ua.DataValue]:
        return await values.read_data_values_per_node(self, node_ids)

    async def read_values_batch_with_fallback(self, node_ids: list[str]) -> list[Any]:
        return await values.read_values_batch_with_fallback(self, node_ids)

    async def read_write_access(self, node_id: str) -> tuple[bool, bool]:
        return await values.read_write_access(self, node_id)

    async def read_history_write_access(self, node_id: str) -> tuple[bool, bool]:
        return await values.read_history_write_access(self, node_id)

    def history_update_supported(self) -> bool | None:
        return self.history_update_support

    async def write_history_value(self, node_id: str, value: Any, quality: str, timestamp: datetime) -> None:
        await values.write_history_value(self, node_id, value, quality, timestamp)

    async def read_variant_type(self, node_id: str) -> str | None:
        return await values.read_variant_type(self, node_id)

    async def write_value(
        self,
        node_id: str,
        value: Any,
        variant_type: str | None,
        quality: str,
        timestamp: datetime,
        verify_metadata: bool = True,
    ) -> None:
        await values.write_value(self, node_id, value, variant_type, quality, timestamp, verify_metadata)

    async def read_history_values(
        self,
        node_ids: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> dict[str, list[DataValueDTO]]:
        history = await values.read_history_values(self, node_ids, start_time, end_time)
        return {
            node_id: [values.to_data_value_dto(value) for value in data_values]
            for node_id, data_values in history.items()
        }

    async def read_history_batch(
        self,
        details: ua.ReadRawModifiedDetails,
        node_ids: list[str],
        continuation_points: list[bytes | None] | None = None,
    ) -> list[ua.HistoryReadResult]:
        return await values.read_history_batch(self, details, node_ids, continuation_points=continuation_points)

    def is_history_missing_or_unsupported(self, exc: Exception) -> bool:
        return values.is_history_missing_or_unsupported(exc)

    async def call_method(self, object_node_id: str, method_node_id: str, args: list[Any]) -> Any:
        return await values.call_method(self, object_node_id, method_node_id, args)

    async def read_server_status_data_value(self) -> DataValueDTO:
        return await values.read_server_status_data_value(self)

    # ------------------------------------------------------------------
    # Recovery seam entry points (recovery.py)
    # ------------------------------------------------------------------

    async def wait_for_reconnect(self, exc: Exception) -> None:
        await recovery.wait_for_reconnect(self, exc)

    def should_retry_after_disconnect(self, exc: Exception) -> bool:
        return recovery.should_retry_after_disconnect(exc)

    def is_transport_read_failure(self, exc: Exception) -> bool:
        return recovery.is_transport_read_failure(exc)

    def is_too_many_operations_error(self, exc: Exception) -> bool:
        return recovery.is_too_many_operations_error(exc)

    def is_continuation_point_invalid(self, exc: Exception) -> bool:
        return recovery.is_continuation_point_invalid(exc)

    async def retry_call(self, awaitable: Awaitable[_T]) -> _T:
        return await recovery.retry_call(self, awaitable)

    # ------------------------------------------------------------------
    # Subscription passthroughs
    # ------------------------------------------------------------------

    async def create_datachange_subscription(
        self, publishing_interval_ms: float, handler: OpcUaDataChangeHandler
    ) -> OpcUaSubscriptionHandle:
        return await self.raw_client.create_subscription(publishing_interval_ms, _NotificationBridge(handler))

    async def subscribe_data_changes(self, subscription: OpcUaSubscriptionHandle, node_ids: list[str]) -> list[int]:
        nodes = [self.raw_node(node_id) for node_id in node_ids]
        # The handle is opaque by contract; its asyncua shape is adapter-internal.
        raw_handles = await cast(Any, subscription).subscribe_data_change(nodes)
        return list(raw_handles)

    async def unsubscribe_data_changes(self, subscription: OpcUaSubscriptionHandle, handles: list[int]) -> None:
        await cast(Any, subscription).unsubscribe(handles)

    async def delete_subscription(self, subscription: OpcUaSubscriptionHandle) -> None:
        await cast(Any, subscription).delete()

    def get_monitored_item_handles(
        self, subscription: OpcUaSubscriptionHandle, node_ids: list[str]
    ) -> dict[str, int] | None:
        """Return the current server handles of `node_ids` in an asyncua-restored subscription.

        asyncua recreates monitored items after a reconnect when the server lost the
        subscription, which assigns new server handles. Returns None if the subscription itself
        is gone; node ids missing from the result are no longer monitored.
        """
        if getattr(subscription, "is_deleted", False) or getattr(subscription, "subscription_id", None) is None:
            return None
        handles_by_node_id: dict[Any, int] = {}
        for item in list(getattr(subscription, "_monitored_items", {}).values()):
            node = getattr(item, "node", None)
            server_handle = getattr(item, "server_handle", None)
            if node is not None and isinstance(server_handle, int):
                handles_by_node_id[node.nodeid] = server_handle
        result: dict[str, int] = {}
        for node_id in node_ids:
            handle = handles_by_node_id.get(self.raw_node(node_id).nodeid)
            if handle is not None:
                result[node_id] = handle
        return result
