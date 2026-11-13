"""OPC UA adapter: connection lifecycle, metadata caching, metrics and entry points.

The transport internals are split into seam modules that each own one concern:
``recovery.py`` (asyncua supervisor, reconnect hand-off, error classification),
``browse.py`` (tree traversal and Browse/BrowseNext plumbing), ``node_reading.py``
(node-info attribute batches), ``metadata.py`` (operational limits, subscription
capabilities, namespaces), ``object_types.py`` (object-type graph and type
hierarchies) and ``values.py`` (reads, writes, history, method calls). Seam
functions take the owning client and call back through its public entry points,
so monkeypatching a client method still intercepts the seam; direct asyncua
bypasses go through the adapter-owned escape hatches (``raw_client``,
``browse_raw``, ``browse_next_raw``, ``raw_node``) instead of private
attributes. Internals are tested in the source module that now owns them
(docs/architecture.md).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
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
)
from i3x_server.infrastructure.opcua import browse, metadata, node_reading, object_types, recovery, values
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
_METADATA_INVALIDATED_AT = float("-inf")

# Caps for the node-id keyed metadata caches below. Entries only expire on read, so without
# a cap a long-lived client accumulates every type it ever browsed. The caps are generous so
# hot base types stay resident in normal deployments and eviction only bounds pathological
# growth.
_TYPE_DEFINITION_HIERARCHY_CACHE_MAX_ENTRIES = 2048
_REFERENCE_TYPE_SUPERTYPES_CACHE_MAX_ENTRIES = 1024

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
        security_mode: str = "None",
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
        self.browse_concurrency = max(1, browse_concurrency)
        # Separate budgets: request handlers fan attribute reads out under the read budget,
        # background refreshes use the smaller refresh budget, and writes keep the
        # transport-level application gate (``application_request_limit`` below).
        self._read_concurrency = max(1, read_concurrency)
        self._refresh_concurrency = max(1, refresh_concurrency)
        self._read_semaphore = asyncio.Semaphore(self._read_concurrency)
        self._refresh_semaphore = asyncio.Semaphore(self._refresh_concurrency)
        self.browse_roots = tuple(browse_roots) if browse_roots is not None else None
        self._metadata_cache_ttl_seconds = max(0, metadata_cache_ttl_seconds)
        self._connection_monitor_interval_seconds = max(1, connection_monitor_interval_seconds)
        self.reconnect_request_timeout_seconds = max(0.1, float(reconnect_request_timeout_seconds))
        self._reconnect_max_delay_seconds = max(1.0, float(reconnect_max_delay_seconds))
        # asyncua's connection supervisor is the single owner of reconnects: it probes the
        # server every watchdog interval, re-activates or recreates the session with backoff,
        # gates new requests while reconnecting, and restores its tracked subscriptions.
        self.raw_client = _SupervisedAsyncuaClient(
            url=endpoint,
            watchdog_intervall=float(self._connection_monitor_interval_seconds),
            reconnect_max_delay=self._reconnect_max_delay_seconds,
            reconnect_request_timeout=self.reconnect_request_timeout_seconds,
            application_request_limit=self.browse_concurrency,
        )
        self.raw_client.reconnected_callback = self._on_asyncua_reconnected
        self.raw_client.application_uri = "urn:i3x2ua:opcua_client"
        self.raw_client.name = "i3x2ua-opcua-client"
        self.raw_client.description = "The i3X API Gateway for OPC UA"
        self.raw_client.product_uri = "urn:github.com/AndreasHeine/i3x2ua.git"
        self._using_user_auth = False
        self._using_security = False
        if self._username is not None and self._password is not None:
            self.raw_client.set_user(self._username)
            self.raw_client.set_password(self._password)
            self._using_user_auth = True
        elif self._username is not None or self._password is not None:
            logger.warning(
                "OPC UA auth config incomplete endpoint=%s; both username and password are "
                "required. Falling back to anonymous session.",
                self.endpoint,
            )
        self._reconnect_listeners: list[Callable[[], Awaitable[None]]] = []
        self.reconnected_event = asyncio.Event()
        self._reconnect_session_recreated = False
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
        self._goodish_quality_labels = {"good", "uncertain"}
        self._connection_state = "Disconnected"
        self._connection_state_since = datetime.now(tz=timezone.utc)
        self._connection_state_task: asyncio.Task[None] | None = None
        self._reconnect_handler_task: asyncio.Task[None] | None = None

    def reset_runtime_metrics(self) -> None:
        self.runtime_metrics = OpcUaRuntimeMetrics()
        self.request_metrics = OpcUaRequestMetrics()

    def snapshot_runtime_metrics(self) -> OpcUaRuntimeMetrics:
        return OpcUaRuntimeMetrics(**asdict(self.runtime_metrics))

    def snapshot_request_metrics(self) -> OpcUaRequestMetrics:
        return OpcUaRequestMetrics(**asdict(self.request_metrics))

    def get_connection_snapshot(self) -> OpcUaConnectionSnapshot:
        return OpcUaConnectionSnapshot(
            state=self._connection_state,
            endpoint=self.endpoint,
            since=self._connection_state_since,
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

    def is_metadata_cache_entry_fresh(self, cached_at: float) -> bool:
        """TTL of 0 means entries never expire, unless explicitly invalidated."""
        if cached_at == _METADATA_INVALIDATED_AT:
            return False
        if self._metadata_cache_ttl_seconds == 0:
            return True
        return perf_counter() - cached_at <= self._metadata_cache_ttl_seconds

    @staticmethod
    def invalidate_metadata_entry(cached: tuple[float, _T] | None) -> tuple[float, _T] | None:
        """Keep the value servable as stale, but force the next access to refresh it."""
        if cached is None:
            return None
        return (_METADATA_INVALIDATED_AT, cached[1])

    def _metadata_dict_cache_read(self, cache: dict[str, tuple[float, _T]], key: str) -> _T | None:
        """Return the fresh value for ``key`` and mark the entry most-recently-used.

        Stale entries are dropped rather than kept: dict-keyed metadata is never served as
        stale, so unreachable entries would only delay eviction.
        """
        entry = cache.get(key)
        if entry is None:
            return None
        if not self.is_metadata_cache_entry_fresh(entry[0]):
            del cache[key]
            return None
        cache[key] = cache.pop(key)
        return entry[1]

    def _metadata_dict_cache_write(
        self, cache: dict[str, tuple[float, _T]], key: str, value: _T, max_entries: int
    ) -> None:
        """Store an entry as most-recently-used and evict least-recently-used ones past the cap."""
        cache[key] = (perf_counter(), value)
        while len(cache) > max_entries:
            del cache[next(iter(cache))]

    def cached_type_definition_hierarchy(self, type_id: str) -> list[OpcUaTypeDefinitionInfo] | None:
        return self._metadata_dict_cache_read(self._type_definition_hierarchy_cache, type_id)

    def store_type_definition_hierarchy(self, type_id: str, hierarchy: list[OpcUaTypeDefinitionInfo]) -> None:
        self._metadata_dict_cache_write(
            self._type_definition_hierarchy_cache,
            type_id,
            hierarchy,
            _TYPE_DEFINITION_HIERARCHY_CACHE_MAX_ENTRIES,
        )

    def cached_reference_type_supertypes(self, type_id: str) -> list[str] | None:
        return self._metadata_dict_cache_read(self._reference_type_supertypes_cache, type_id)

    def store_reference_type_supertypes(self, type_id: str, names: list[str]) -> None:
        self._metadata_dict_cache_write(
            self._reference_type_supertypes_cache,
            type_id,
            names,
            _REFERENCE_TYPE_SUPERTYPES_CACHE_MAX_ENTRIES,
        )

    def _start_metadata_load(self, key: str, loader: Callable[[], Awaitable[_T]]) -> asyncio.Future[_T]:
        task = self.metadata_load_tasks.get(key)
        if task is None:
            task = asyncio.ensure_future(loader())
            self.metadata_load_tasks[key] = task
            task.add_done_callback(lambda done: self._finish_metadata_load(key, done))
        return cast("asyncio.Future[_T]", task)

    async def _coalesce_metadata_load(self, key: str, loader: Callable[[], Awaitable[_T]]) -> _T:
        """Share one in-flight OPC UA metadata load between concurrent callers.

        Without this, every request that sees an expired cache entry starts its own full
        browse, which overloads the OPC UA server under concurrent load.
        """
        return await asyncio.shield(self._start_metadata_load(key, loader))

    async def _get_cached_metadata(
        self,
        key: str,
        cached: tuple[float, _T] | None,
        loader: Callable[[], Awaitable[_T]],
    ) -> _T:
        """Serve stale metadata while one shared background load refreshes it.

        Only a cold cache waits for OPC UA, so TTL expiry never stalls API requests.
        """
        if cached is None:
            return await self._coalesce_metadata_load(key, loader)
        cached_at, cached_value = cached
        if not self.is_metadata_cache_entry_fresh(cached_at):
            incident = self.stale_metadata_incidents.get(key)
            if incident is None:
                self.stale_metadata_incidents[key] = (perf_counter(), 0)
                logger.warning(
                    "OPC UA serving stale metadata while refreshing endpoint=%s key=%s age_s=%s ttl_s=%s",
                    self.endpoint,
                    key,
                    "invalidated" if cached_at == _METADATA_INVALIDATED_AT else f"{perf_counter() - cached_at:.3f}",
                    self._metadata_cache_ttl_seconds,
                )
            else:
                started_at, suppressed_events = incident
                self.stale_metadata_incidents[key] = (started_at, suppressed_events + 1)
            self._start_metadata_load(key, loader)
        return cached_value

    def _finish_metadata_load(self, key: str, task: asyncio.Future[Any]) -> None:
        if self.metadata_load_tasks.get(key) is task:
            del self.metadata_load_tasks[key]
        if task.cancelled() or task.exception() is not None:
            return

        incident = self.stale_metadata_incidents.pop(key, None)
        if incident is not None:
            started_at, suppressed_events = incident
            logger.info(
                "OPC UA metadata refresh recovered endpoint=%s key=%s stale_duration_s=%.3f suppressed_events=%d",
                self.endpoint,
                key,
                perf_counter() - started_at,
                suppressed_events,
            )

    async def connect(self) -> None:
        started = perf_counter()
        security_started = perf_counter()
        await self._configure_security_if_needed()
        security_duration_s = perf_counter() - security_started
        logger.info(
            "OPC UA connect started endpoint=%s auth_mode=%s security_mode=%s",
            self.endpoint,
            "userpass" if self._using_user_auth else "anonymous",
            self._security_mode,
        )
        session_started = perf_counter()
        # Client.disconnect() clears asyncua's request gate and never restores it on connect().
        self.raw_client.uaclient.pre_request_hook = self.raw_client._wait_until_ready
        await self.raw_client.connect(
            auto_reconnect=True,
            reconnect_max_delay=self._reconnect_max_delay_seconds,
            reconnect_request_timeout=self.reconnect_request_timeout_seconds,
        )
        self._set_connection_state("Connected")
        self._start_connection_tasks()
        session_duration_s = perf_counter() - session_started
        typedef_started = perf_counter()
        await self.load_additional_typedefinitions()
        typedef_duration_s = perf_counter() - typedef_started
        limits_started = perf_counter()
        self.limits_cache = await self.get_operational_limits()
        limits_duration_s = perf_counter() - limits_started
        logger.info(
            "OPC UA limits endpoint=%s max_nodes_per_browse=%s max_nodes_per_read=%s",
            self.endpoint,
            self.limits_cache.max_nodes_per_browse,
            self.limits_cache.max_nodes_per_read,
        )
        logger.info(
            (
                "OPC UA connect phases endpoint=%s security_s=%.3f session_s=%.3f "
                "typedef_s=%.3f limits_s=%.3f total_s=%.3f"
            ),
            self.endpoint,
            security_duration_s,
            session_duration_s,
            typedef_duration_s,
            limits_duration_s,
            perf_counter() - started,
        )

    async def load_additional_typedefinitions(self) -> None:
        started = perf_counter()
        logger.info("OPC UA additional type definitions load started endpoint=%s", self.endpoint)
        try:
            await self.raw_client.load_data_type_definitions()
        except Exception as e:
            logger.warning(
                "OPC UA additional v1.04 data type definitions load failed endpoint=%s error=%s", self.endpoint, e
            )
            try:
                await self.raw_client.load_type_definitions()
            except Exception as e:
                logger.warning(
                    "OPC UA additional v1.03 data type definitions load failed endpoint=%s error=%s", self.endpoint, e
                )
            else:
                logger.info(
                    "OPC UA additional type definitions v1.03 load finished endpoint=%s duration_s=%.3f",
                    self.endpoint,
                    perf_counter() - started,
                )
        else:
            logger.info(
                "OPC UA additional type definitions load finished endpoint=%s duration_s=%.3f",
                self.endpoint,
                perf_counter() - started,
            )

    async def disconnect(self) -> None:
        started = perf_counter()
        logger.info("OPC UA disconnect started endpoint=%s", self.endpoint)
        await self._stop_connection_tasks()
        # Drop reconnect bookkeeping: a later `connect()` must not run the reconnect
        # handler for a stale event or re-fire listeners with a recreated-session flag (L10).
        self.reconnected_event.clear()
        self._reconnect_session_recreated = False
        await self.raw_client.disconnect()
        self._set_connection_state("Disconnected")
        self.limits_cache = None
        self.namespace_infos_cache = None
        self.object_type_cache = None
        self.subscription_caps_cache = None
        self._reference_type_supertypes_cache = {}
        self._type_definition_hierarchy_cache = {}
        logger.info("OPC UA disconnect finished endpoint=%s duration_s=%.3f", self.endpoint, perf_counter() - started)

    def _start_connection_tasks(self) -> None:
        if self._connection_state_task is None or self._connection_state_task.done():
            self._connection_state_task = asyncio.create_task(self._watch_connection_state())
        if self._reconnect_handler_task is None or self._reconnect_handler_task.done():
            self._reconnect_handler_task = asyncio.create_task(self._reconnect_handler_loop())

    async def _stop_connection_tasks(self) -> None:
        tasks = [self._connection_state_task, self._reconnect_handler_task]
        self._connection_state_task = None
        self._reconnect_handler_task = None
        for task in tasks:
            if task is None:
                continue
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _watch_connection_state(self) -> None:
        """Mirror asyncua's transport state into the connection snapshot."""
        uaclient = self.raw_client.uaclient
        async with uaclient.subscribe_state() as states:
            self._apply_client_state(uaclient.state)
            while True:
                self._apply_client_state(await states.next_change())

    def _apply_client_state(self, state: UaClientState) -> None:
        if state is UaClientState.CONNECTED:
            self._set_connection_state("Connected")
            return
        if self.raw_client.uaclient.is_disconnect_requested:
            self._set_connection_state("Disconnected")
            return
        if self._connection_state == "Connected":
            logger.warning(
                "OPC UA connection lost endpoint=%s client_state=%s; waiting for asyncua reconnect",
                self.endpoint,
                state.value,
            )
        self._set_connection_state("Reconnecting")

    def _on_asyncua_reconnected(self, session_reused: bool) -> None:
        """Called by the asyncua supervisor once the session and its subscriptions are restored."""
        self._set_connection_state("Connected")
        self.limits_cache = None
        self.subscription_caps_cache = None
        # Keep the expensive browse results servable as stale so API requests do not
        # block on a cold rebuild while the server is recovering from the reconnect.
        self.namespace_infos_cache = self.invalidate_metadata_entry(self.namespace_infos_cache)
        self.object_type_cache = self.invalidate_metadata_entry(self.object_type_cache)
        self._reference_type_supertypes_cache = {}
        self._type_definition_hierarchy_cache = {}
        self._reconnect_session_recreated = self._reconnect_session_recreated or not session_reused
        logger.info(
            "OPC UA reconnect finished endpoint=%s session=%s",
            self.endpoint,
            "reused" if session_reused else "recreated",
        )
        # The follow-up work runs in our own task: the supervisor must not block on it, and
        # requests issued from the supervisor's context would bypass asyncua's request gate.
        self.reconnected_event.set()

    async def _reconnect_handler_loop(self) -> None:
        while True:
            await self.reconnected_event.wait()
            self.reconnected_event.clear()
            await self._handle_reconnected()

    async def _handle_reconnected(self) -> None:
        if self._reconnect_session_recreated:
            self._reconnect_session_recreated = False
            await self.load_additional_typedefinitions()
        for listener in list(self._reconnect_listeners):
            try:
                await listener()
            except Exception:
                logger.exception("OPC UA reconnect listener failed endpoint=%s", self.endpoint)

    def add_reconnect_listener(self, listener: Callable[[], Awaitable[None]]) -> None:
        self._reconnect_listeners.append(listener)

    def remove_reconnect_listener(self, listener: Callable[[], Awaitable[None]]) -> None:
        with suppress(ValueError):
            self._reconnect_listeners.remove(listener)

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

    # ------------------------------------------------------------------
    # Security configuration
    # ------------------------------------------------------------------

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
        await self.raw_client.set_security_string(security_string)
        self._using_security = True

    # ------------------------------------------------------------------
    # Connection state and request metrics
    # ------------------------------------------------------------------

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

    def record_read_success(self) -> None:
        self.request_metrics.read_count += 1

    def record_history_read_success(self) -> None:
        self.request_metrics.history_read_count += 1

    def record_method_success(self) -> None:
        self.request_metrics.method_call_count += 1

    def record_failed_request(self) -> None:
        self.request_metrics.failed_request_count += 1

    def record_browse_results(self, results: list[ua.BrowseResult]) -> None:
        if not results:
            self.record_failed_request()
            return
        if all(self._is_goodish_status(getattr(result, "StatusCode", None)) for result in results):
            self.request_metrics.browse_count += 1
            return
        self.record_failed_request()

    def record_read_data_values(self, values: list[ua.DataValue]) -> None:
        if not values:
            self.record_failed_request()
            return
        if all(self._is_goodish_status(value.StatusCode) for value in values):
            self.record_read_success()
            return
        self.record_failed_request()


def _assert_file_exists(path: Path, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")
