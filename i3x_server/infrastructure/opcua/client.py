from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import asdict, fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, TypeVar, cast

from asyncua import ua
from asyncua.client.client import Client, _supervisor_owns_requests
from asyncua.client.ua_client import UaClient, UaClientState
from asyncua.common.node import Node
from asyncua.common.shortcuts import Shortcuts
from asyncua.common.utils import Buffer
from asyncua.ua import NodeClass
from asyncua.ua.attribute_ids import AttributeIds
from asyncua.ua.object_ids import ObjectIds
from asyncua.ua.status_codes import StatusCodes
from asyncua.ua.uaerrors import UaError, UaStatusCodeError

from i3x_server.domain.ports.opcua import (
    OpcUaClientProtocol,
    OpcUaConnectionSnapshot,
    OpcUaNamespaceInfo,
    OpcUaNodeInfo,
    OpcUaObjectTypeInfo,
    OpcUaObjectTypeMemberInfo,
    OpcUaOperationalLimits,
    OpcUaReferenceInfo,
    OpcUaRequestMetrics,
    OpcUaRuntimeMetrics,
    OpcUaSubscriptionCapabilities,
    OpcUaTypeDefinitionInfo,
    OpcUaWriteMetadataUnsupportedError,
    OpcUaWriteMetadataVerificationError,
)
from i3x_server.infrastructure.opcua.history import historical_data_value, quality_status_code

__all__ = [
    "OpcUaClient",
    "OpcUaClientProtocol",
    "OpcUaConnectionSnapshot",
    "OpcUaNamespaceInfo",
    "OpcUaNodeInfo",
    "OpcUaObjectTypeInfo",
    "OpcUaObjectTypeMemberInfo",
    "OpcUaOperationalLimits",
    "OpcUaReferenceInfo",
    "OpcUaRequestMetrics",
    "OpcUaRuntimeMetrics",
    "OpcUaSubscriptionCapabilities",
    "OpcUaTypeDefinitionInfo",
]

logger = logging.getLogger(__name__)

_T = TypeVar("_T")
_METADATA_INVALIDATED_AT = float("-inf")
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
# Status codes meaning the session or secure channel is gone; asyncua's supervisor must
# re-establish it before the request can succeed.
_CONNECTION_LOST_STATUS_CODES = frozenset(
    {
        StatusCodes.BadSessionClosed,
        StatusCodes.BadSessionIdInvalid,
        StatusCodes.BadSessionNotActivated,
        StatusCodes.BadSecureChannelClosed,
        StatusCodes.BadSecureChannelIdInvalid,
        StatusCodes.BadConnectionClosed,
        StatusCodes.BadNotConnected,
        StatusCodes.BadServerNotConnected,
        StatusCodes.BadCommunicationError,
    }
)
_CONNECTION_LOST_MARKERS = (
    "connection is closed",
    "connection is not open",
    "connection reset",
    "broken pipe",
    "transport closed",
    "client is disconnected",
    "client is disconnecting",
    "badsessionclosed",
    "badsessionidinvalid",
    "badsecurechannelclosed",
)


class _RecoveryUaClient(UaClient):
    """Reserve the recovery path independently of the application request budget."""

    async def _send_request(
        self,
        request: Any,
        timeout: float | None = None,
        # asyncua annotates byte-valued MessageType constants as MessageType.
        message_type: ua.MessageType = cast(ua.MessageType, ua.MessageType.SecureMessage),
    ) -> Buffer:
        supervisor_request = _supervisor_owns_requests.get()
        # Readiness must be checked before acquiring a slot, not only in the protocol hook.
        if not supervisor_request and self.pre_request_hook is not None:
            await self.pre_request_hook()
        with self.observer.observe_request(request):
            # Publish is long-lived and must not consume the only slot when the budget is one.
            if supervisor_request or isinstance(request, ua.PublishRequest):
                if self.protocol is None:
                    raise ConnectionError("Connection is not open")
                return await self.protocol.send_request(request, timeout, message_type)
            request_timeout = self._timeout if timeout is None else timeout
            return await asyncio.wait_for(
                self._send_application_request(request, timeout, message_type), timeout=request_timeout or None
            )

    async def _send_application_request(
        self, request: Any, timeout: float | None, message_type: ua.MessageType
    ) -> Buffer:
        async with self._request_semaphore:
            if self.protocol is None:
                raise ConnectionError("Connection is not open")
            return await self.protocol.send_request(request, timeout, message_type)


class _SupervisedAsyncuaClient(Client):
    """asyncua client that reports when its connection supervisor completed a reconnect.

    asyncua is pinned to 2.1.0: the request scheduler and supervisor context are
    private integration points, as is the post-subscription `_finalize_reconnect` hook.
    """

    reconnected_callback: Callable[[bool], None] | None = None

    def __init__(
        self,
        url: str,
        *,
        watchdog_intervall: float,
        reconnect_max_delay: float,
        reconnect_request_timeout: float,
        application_request_limit: int,
    ) -> None:
        super().__init__(
            url,
            watchdog_intervall=watchdog_intervall,
            reconnect_max_delay=reconnect_max_delay,
            reconnect_request_timeout=reconnect_request_timeout,
        )
        self.uaclient = _RecoveryUaClient(timeout=self.uaclient._timeout)
        self.uaclient.set_max_concurrent_requests(application_request_limit)
        self.uaclient.pre_request_hook = self._wait_until_ready
        self.nodes = Shortcuts(self.uaclient.session)

    async def _wait_until_ready(self) -> None:
        if (
            not _supervisor_owns_requests.get()
            and self._supervisor_task is not None
            and self.uaclient.state in {UaClientState.CONNECTING, UaClientState.SOCKET_OPEN, UaClientState.CHANNEL_OPEN}
        ):
            async with self.uaclient.subscribe_state() as states:
                try:
                    await states.wait_for_state(UaClientState.CONNECTED, timeout=self._reconnect_request_timeout)
                except TimeoutError:
                    raise ConnectionError(
                        f"Timed out waiting for client to reconnect (state={self.uaclient.state.value})"
                    ) from None
            return
        await super()._wait_until_ready()

    async def _try_reactivate_existing_session(self) -> bool:
        try:
            return await asyncio.wait_for(
                super()._try_reactivate_existing_session(), timeout=self._reconnect_request_timeout
            )
        except TimeoutError:
            self.disconnect_socket()
            raise

    async def _recreate_subscriptions(self) -> None:
        self._subscriptions = [sub for sub in self._subscriptions if not sub.is_deleted]
        for sub in self._subscriptions:
            try:
                try:
                    await asyncio.wait_for(sub.restore(), timeout=self._reconnect_request_timeout)
                except TimeoutError:
                    logger.warning(
                        "OPC UA subscription restore timed out subscription_id=%s; recreating",
                        sub.subscription_id,
                    )
                    await asyncio.wait_for(sub.recreate(), timeout=self._reconnect_request_timeout)
            except Exception:
                logger.exception("OPC UA subscription recovery failed subscription_id=%s", sub.subscription_id)

    async def _finalize_reconnect(self, reused: bool) -> None:
        await super()._finalize_reconnect(reused)
        callback = self.reconnected_callback
        if callback is not None:
            callback(reused)


def _normalize_type_definition_id(type_definition_id: str | None) -> str | None:
    if not type_definition_id:
        return None
    normalized = type_definition_id.strip()
    if normalized in {"i=0", "ns=0;i=0", "nsu=http://opcfoundation.org/UA/;i=0"}:
        return None
    return normalized


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
        connection_monitor_interval_seconds: int = 5,
        reconnect_request_timeout_seconds: float = 10.0,
        reconnect_max_delay_seconds: float = 30.0,
        browse_roots: list[str] | None = None,
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
        self._browse_roots = tuple(browse_roots) if browse_roots is not None else None
        self._metadata_cache_ttl_seconds = max(0, metadata_cache_ttl_seconds)
        self._connection_monitor_interval_seconds = max(1, connection_monitor_interval_seconds)
        self._reconnect_request_timeout_seconds = max(0.1, float(reconnect_request_timeout_seconds))
        self._reconnect_max_delay_seconds = max(1.0, float(reconnect_max_delay_seconds))
        # asyncua's connection supervisor is the single owner of reconnects: it probes the
        # server every watchdog interval, re-activates or recreates the session with backoff,
        # gates new requests while reconnecting, and restores its tracked subscriptions.
        self._client = _SupervisedAsyncuaClient(
            url=endpoint,
            watchdog_intervall=float(self._connection_monitor_interval_seconds),
            reconnect_max_delay=self._reconnect_max_delay_seconds,
            reconnect_request_timeout=self._reconnect_request_timeout_seconds,
            application_request_limit=min(16, self._browse_concurrency),
        )
        self._client.reconnected_callback = self._on_asyncua_reconnected
        self._client.application_uri = "urn:i3x2ua:opcua_client"
        self._client.name = "i3x2ua-opcua-client"
        self._client.description = "The i3X API Gateway for OPC UA"
        self._client.product_uri = "urn:github.com/AndreasHeine/i3x2ua.git"
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
        self._reconnect_listeners: list[Callable[[], Awaitable[None]]] = []
        self._reconnected_event = asyncio.Event()
        self._reconnect_session_recreated = False
        self._limits_cache: OpcUaOperationalLimits | None = None
        self._subscription_caps_cache: OpcUaSubscriptionCapabilities | None = None
        self._namespace_infos_cache: tuple[float, list[OpcUaNamespaceInfo]] | None = None
        self._object_types_cache: tuple[float, list[OpcUaObjectTypeInfo]] | None = None
        self._metadata_load_tasks: dict[str, asyncio.Future[Any]] = {}
        self._stale_metadata_incidents: dict[str, tuple[float, int]] = {}
        self._reference_type_supertypes_cache: dict[str, tuple[float, list[str]]] = {}
        self._type_definition_hierarchy_cache: dict[str, tuple[float, list[OpcUaTypeDefinitionInfo]]] = {}
        self._runtime_metrics = OpcUaRuntimeMetrics()
        self._history_update_supported: bool | None = None
        self._request_metrics = OpcUaRequestMetrics()
        self._goodish_quality_labels = {"good", "uncertain"}
        self._connection_state = "Disconnected"
        self._connection_state_since = datetime.now(tz=timezone.utc)
        self._connection_state_task: asyncio.Task[None] | None = None
        self._reconnect_handler_task: asyncio.Task[None] | None = None

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

    def _is_metadata_cache_entry_fresh(self, cached_at: float) -> bool:
        """TTL of 0 means entries never expire, unless explicitly invalidated."""
        if cached_at == _METADATA_INVALIDATED_AT:
            return False
        if self._metadata_cache_ttl_seconds == 0:
            return True
        return perf_counter() - cached_at <= self._metadata_cache_ttl_seconds

    @staticmethod
    def _invalidate_metadata_entry(cached: tuple[float, _T] | None) -> tuple[float, _T] | None:
        """Keep the value servable as stale, but force the next access to refresh it."""
        if cached is None:
            return None
        return (_METADATA_INVALIDATED_AT, cached[1])

    def _start_metadata_load(self, key: str, loader: Callable[[], Awaitable[_T]]) -> asyncio.Future[_T]:
        task = self._metadata_load_tasks.get(key)
        if task is None:
            task = asyncio.ensure_future(loader())
            self._metadata_load_tasks[key] = task
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
        if not self._is_metadata_cache_entry_fresh(cached_at):
            incident = self._stale_metadata_incidents.get(key)
            if incident is None:
                self._stale_metadata_incidents[key] = (perf_counter(), 0)
                logger.warning(
                    "OPC UA serving stale metadata while refreshing endpoint=%s key=%s age_s=%s ttl_s=%s",
                    self._endpoint,
                    key,
                    "invalidated" if cached_at == _METADATA_INVALIDATED_AT else f"{perf_counter() - cached_at:.3f}",
                    self._metadata_cache_ttl_seconds,
                )
            else:
                started_at, suppressed_events = incident
                self._stale_metadata_incidents[key] = (started_at, suppressed_events + 1)
            self._start_metadata_load(key, loader)
        return cached_value

    def _finish_metadata_load(self, key: str, task: asyncio.Future[Any]) -> None:
        if self._metadata_load_tasks.get(key) is task:
            del self._metadata_load_tasks[key]
        if task.cancelled() or task.exception() is not None:
            return

        incident = self._stale_metadata_incidents.pop(key, None)
        if incident is not None:
            started_at, suppressed_events = incident
            logger.info(
                "OPC UA metadata refresh recovered endpoint=%s key=%s stale_duration_s=%.3f suppressed_events=%d",
                self._endpoint,
                key,
                perf_counter() - started_at,
                suppressed_events,
            )

    async def resolve_reference_type_supertype_browse_names(self, reference_type_id: str) -> list[str]:
        if not isinstance(reference_type_id, str) or not reference_type_id:
            return []
        return await self._resolve_reference_type_chain(reference_type_id, set())

    async def resolve_type_definition_hierarchy(
        self,
        type_definition_id: str,
    ) -> list[OpcUaTypeDefinitionInfo]:
        if not isinstance(type_definition_id, str) or not type_definition_id:
            return []
        hierarchies = await self.resolve_type_definition_hierarchies([type_definition_id])
        return hierarchies[type_definition_id]

    async def resolve_type_definition_hierarchies(
        self,
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
                cached = self._type_definition_hierarchy_cache.get(type_id)
                if cached is not None and self._is_metadata_cache_entry_fresh(cached[0]):
                    cached_chains[type_id] = list(cached[1])
                    cache_hits += 1
                else:
                    unresolved.append(type_id)
            if not unresolved:
                break
            if limits is None:
                limits = await self.get_operational_limits()
            nodes = [self._client.get_node(type_id) for type_id in unresolved]
            for node_batch in _chunked_nodes(nodes, limits.max_nodes_per_read or 128):
                values = await self._read_attribute_batch(node_batch, AttributeIds.BrowseName)
                for node, value in zip(node_batch, values, strict=True):
                    type_id = node.nodeid.to_string()
                    qualified_name = self._extract_attribute_value(value)
                    if not isinstance(qualified_name, ua.QualifiedName) or not qualified_name.Name:
                        raise ValueError(f"Cannot resolve type BrowseName for {type_id}: {value.StatusCode}")
                    type_infos[type_id] = OpcUaTypeDefinitionInfo(type_id, qualified_name.Name)
            browsed = await self._browse_references_descriptions(
                nodes,
                max_nodes_per_browse=limits.max_nodes_per_browse or 128,
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
            self._type_definition_hierarchy_cache[root_id] = (perf_counter(), list(hierarchy))
        logger.info(
            "OPC UA type hierarchy resolution endpoint=%s requested=%d cache_hits=%d resolved_types=%d duration_s=%.3f",
            self._endpoint,
            len(requested_ids),
            cache_hits,
            len(type_infos),
            perf_counter() - started,
        )
        return {type_id: list(hierarchies[type_id]) for type_id in requested_ids}

    async def _resolve_reference_type_chain(self, type_id: str, in_progress: set[str]) -> list[str]:
        """Browse names of ``type_id`` followed by its supertypes, nearest ancestor first.

        Every node reached is cached in its own right, so sibling reference types that
        converge on shared ancestors resolve those ancestors only once.
        """
        cached = self._reference_type_supertypes_cache.get(type_id)
        if cached is not None:
            cached_at, cached_names = cached
            if self._is_metadata_cache_entry_fresh(cached_at):
                return list(cached_names)
            del self._reference_type_supertypes_cache[type_id]

        if type_id in in_progress:
            return []
        in_progress.add(type_id)

        discovered_names: list[str] = []
        seen_names: set[str] = set()
        type_node = self._client.get_node(type_id)

        try:
            browse_name_obj = await type_node.read_browse_name()
            browse_name = getattr(browse_name_obj, "Name", None)
            if isinstance(browse_name, str) and browse_name:
                seen_names.add(browse_name)
                discovered_names.append(browse_name)
        except Exception:
            logger.debug(
                "OPC UA browse name read failed for reference type endpoint=%s reference_type_id=%s",
                self._endpoint,
                type_id,
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
                type_id,
                exc_info=True,
            )
            supertype_refs_by_node = []

        for _, refs in supertype_refs_by_node:
            for ref in refs:
                supertype_id = ref.NodeId.to_string()
                if not isinstance(supertype_id, str) or not supertype_id:
                    continue
                for name in await self._resolve_reference_type_chain(supertype_id, in_progress):
                    if name not in seen_names:
                        seen_names.add(name)
                        discovered_names.append(name)

        in_progress.discard(type_id)
        self._reference_type_supertypes_cache[type_id] = (perf_counter(), list(discovered_names))
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
        # Client.disconnect() clears asyncua's request gate and never restores it on connect().
        self._client.uaclient.pre_request_hook = self._client._wait_until_ready
        await self._client.connect(
            auto_reconnect=True,
            reconnect_max_delay=self._reconnect_max_delay_seconds,
            reconnect_request_timeout=self._reconnect_request_timeout_seconds,
        )
        self._set_connection_state("Connected")
        self._start_connection_tasks()
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
        await self._stop_connection_tasks()
        await self._client.disconnect()
        self._set_connection_state("Disconnected")
        self._limits_cache = None
        self._namespace_infos_cache = None
        self._object_types_cache = None
        self._subscription_caps_cache = None
        self._reference_type_supertypes_cache = {}
        self._type_definition_hierarchy_cache = {}
        logger.info("OPC UA disconnect finished endpoint=%s duration_s=%.3f", self._endpoint, perf_counter() - started)

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
        uaclient = self._client.uaclient
        async with uaclient.subscribe_state() as states:
            self._apply_client_state(uaclient.state)
            while True:
                self._apply_client_state(await states.next_change())

    def _apply_client_state(self, state: UaClientState) -> None:
        if state is UaClientState.CONNECTED:
            self._set_connection_state("Connected")
            return
        if self._client.uaclient.is_disconnect_requested:
            self._set_connection_state("Disconnected")
            return
        if self._connection_state == "Connected":
            logger.warning(
                "OPC UA connection lost endpoint=%s client_state=%s; waiting for asyncua reconnect",
                self._endpoint,
                state.value,
            )
        self._set_connection_state("Reconnecting")

    def _on_asyncua_reconnected(self, session_reused: bool) -> None:
        """Called by the asyncua supervisor once the session and its subscriptions are restored."""
        self._set_connection_state("Connected")
        self._limits_cache = None
        self._subscription_caps_cache = None
        # Keep the expensive browse results servable as stale so API requests do not
        # block on a cold rebuild while the server is recovering from the reconnect.
        self._namespace_infos_cache = self._invalidate_metadata_entry(self._namespace_infos_cache)
        self._object_types_cache = self._invalidate_metadata_entry(self._object_types_cache)
        self._reference_type_supertypes_cache = {}
        self._type_definition_hierarchy_cache = {}
        self._reconnect_session_recreated = self._reconnect_session_recreated or not session_reused
        logger.info(
            "OPC UA reconnect finished endpoint=%s session=%s",
            self._endpoint,
            "reused" if session_reused else "recreated",
        )
        # The follow-up work runs in our own task: the supervisor must not block on it, and
        # requests issued from the supervisor's context would bypass asyncua's request gate.
        self._reconnected_event.set()

    async def _reconnect_handler_loop(self) -> None:
        while True:
            await self._reconnected_event.wait()
            self._reconnected_event.clear()
            await self._handle_reconnected()

    async def _handle_reconnected(self) -> None:
        if self._reconnect_session_recreated:
            self._reconnect_session_recreated = False
            await self.load_additional_typedefinitions()
        for listener in list(self._reconnect_listeners):
            try:
                await listener()
            except Exception:
                logger.exception("OPC UA reconnect listener failed endpoint=%s", self._endpoint)

    async def _resolve_browse_roots(self) -> list[Node]:
        if self._browse_roots is None:
            return [self._client.nodes.objects]
        if not self._browse_roots:
            raise ValueError("Configured OPC UA browse roots must not be empty")
        namespace_array = await self._client.get_namespace_array()
        roots: dict[str, Node] = {}
        for configured_id in self._browse_roots:
            expanded = ua.NodeId.from_string(configured_id)
            if not isinstance(expanded, ua.ExpandedNodeId) or not expanded.NamespaceUri or expanded.ServerIndex:
                raise ValueError(f"Browse root must be a local URI-based ExpandedNodeId: {configured_id!r}")
            try:
                namespace_index = namespace_array.index(expanded.NamespaceUri)
            except ValueError as exc:
                raise ValueError(
                    f"Browse root namespace is not available on {self._endpoint}: {configured_id!r}"
                ) from exc
            node_id = ua.NodeId(expanded.Identifier, ua.Int16(namespace_index), expanded.NodeIdType)
            node = self._client.get_node(node_id)
            try:
                node_class = await node.read_node_class()
            except UaStatusCodeError as exc:
                raise ValueError(f"Cannot read configured OPC UA browse root {configured_id!r}: {exc}") from exc
            if node_class != NodeClass.Object:
                raise ValueError(f"Configured OPC UA browse root is not an Object: {configured_id!r}")
            roots[node_id.to_string()] = node
        logger.info(
            "OPC UA browse roots resolved endpoint=%s configured=%s resolved=%s",
            self._endpoint,
            self._browse_roots,
            list(roots),
        )
        return list(roots.values())

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
        roots = await self._resolve_browse_roots()
        output: list[OpcUaNodeInfo] = []
        visited: set[str] = set()
        reference_type_names: dict[str, str] = {}
        stack: list[tuple[Any, str | None]] = [(root, None) for root in roots]

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

            nodes = [node for node, _ in filtered_entries]
            node_infos, all_refs, browsed = await asyncio.gather(
                self._read_node_infos_limited(filtered_entries),
                self._browse_references_descriptions(
                    nodes,
                    max_nodes_per_browse=max_nodes_per_browse,
                    reference_type_id=ObjectIds.References,
                ),
                self._browse_children_descriptions(nodes, max_nodes_per_browse),
            )
            output.extend(node_infos)

            await self._read_reference_type_browse_names(
                [ref for _, refs in all_refs for ref in refs], reference_type_names
            )
            node_info_by_id = {item.node_id: item for item in node_infos}
            for parent_node, refs in all_refs:
                info = node_info_by_id.get(parent_node.nodeid.to_string())
                if info is None:
                    continue
                info.outgoing_references = self._to_reference_infos(refs, reference_type_names)
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

    async def _read_reference_type_browse_names(
        self, refs: list[ua.ReferenceDescription], names: dict[str, str]
    ) -> None:
        type_ids = sorted({ref.ReferenceTypeId.to_string() for ref in refs} - names.keys())
        if not type_ids:
            return
        nodes = [self._client.get_node(type_id) for type_id in type_ids]
        values = await self._read_attribute_batch_limited(nodes, AttributeIds.BrowseName)
        for type_id, value in zip(type_ids, values, strict=True):
            qualified_name = self._extract_attribute_value(value)
            browse_name = getattr(qualified_name, "Name", None)
            if not isinstance(browse_name, str) or not browse_name:
                raise ValueError(f"Cannot read BrowseName for reference type {type_id!r}")
            names[type_id] = browse_name

    def _to_reference_infos(
        self, refs: list[ua.ReferenceDescription], reference_type_names: dict[str, str]
    ) -> list[OpcUaReferenceInfo]:
        output: list[OpcUaReferenceInfo] = []
        for ref in refs:
            target_node_id = ref.NodeId.to_string()
            reference_type_id = ref.ReferenceTypeId.to_string()
            reference_browse_name = reference_type_names[reference_type_id]
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
        attribute_values = await self._read_attribute_pairs([(node, attr) for attr in _NODE_INFO_ATTRIBUTES])
        values_by_attribute = dict(zip(_NODE_INFO_ATTRIBUTES, attribute_values, strict=True))
        browse_name_obj = self._extract_attribute_value(values_by_attribute[AttributeIds.BrowseName])
        display_name_obj = self._extract_attribute_value(values_by_attribute[AttributeIds.DisplayName])
        node_class_value = self._extract_attribute_value(values_by_attribute[AttributeIds.NodeClass])
        if browse_name_obj is None or display_name_obj is None:
            raise ValueError(f"Cannot read required node names for OPC UA node {node_id}")
        node_class_obj = self._coerce_node_class(node_class_value)
        if node_class_obj is None:
            raise ValueError(f"Cannot read node class for OPC UA node {node_id}")
        description_value = self._extract_attribute_value(values_by_attribute[AttributeIds.Description])
        description_text = getattr(description_value, "Text", None)
        description = description_text if isinstance(description_text, str) and description_text.strip() else None
        class_values = {
            attr: self._extract_class_node_info_attribute(
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
                    self._endpoint,
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

    async def _read_node_infos_limited(self, entries: list[tuple[Any, str | None]]) -> list[OpcUaNodeInfo]:
        if not entries:
            return []

        limits = await self.get_operational_limits()
        max_nodes = limits.max_nodes_per_read or len(entries)
        batch_size = max(1, min(max_nodes // len(_NODE_INFO_ATTRIBUTES), len(entries)))
        output: list[OpcUaNodeInfo] = []

        for entry_batch in _chunked_nodes(entries, batch_size):
            nodes = [node for node, _ in entry_batch]
            parent_by_node_id = {node.nodeid.to_string(): parent_node_id for node, parent_node_id in entry_batch}

            try:
                node_attributes = [(node, attr) for node in nodes for attr in _NODE_INFO_ATTRIBUTES]
                attribute_values = await self._read_attribute_pairs(node_attributes)
            except Exception:
                logger.warning(
                    "OPC UA batch node read failed endpoint=%s batch_size=%d; falling back to per-node reads",
                    self._endpoint,
                    len(entry_batch),
                    exc_info=True,
                )
                output.extend(await self._read_node_infos_fallback(entry_batch))
                continue

            object_or_variable_nodes: list[Any] = []
            mandatory_by_node_id: dict[str, tuple[str, str, NodeClass]] = {}
            descriptions_by_node_id: dict[str, str] = {}
            class_attributes_by_node_id: dict[str, dict[AttributeIds, Any]] = {}
            fallback_entries: list[tuple[Any, str | None]] = []

            for index, node in enumerate(nodes):
                node_id = node.nodeid.to_string()
                start = index * len(_NODE_INFO_ATTRIBUTES)
                node_values = {
                    attr: attribute_values[start + offset] for offset, attr in enumerate(_NODE_INFO_ATTRIBUTES)
                }
                browse_name_value = self._extract_attribute_value(node_values[AttributeIds.BrowseName])
                display_name_value = self._extract_attribute_value(node_values[AttributeIds.DisplayName])
                node_class_value = self._extract_attribute_value(node_values[AttributeIds.NodeClass])
                node_class = self._coerce_node_class(node_class_value)
                if node_class is None:
                    fallback_entries.append((node, parent_by_node_id[node_id]))
                    continue

                description_value = self._extract_attribute_value(node_values[AttributeIds.Description])
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
                    attr: self._extract_class_node_info_attribute(node_id, node_class, attr, node_values[attr])
                    for attr in applicable_attributes
                }
                if node_class in {NodeClass.Object, NodeClass.Variable}:
                    object_or_variable_nodes.append(node)

            type_definitions_by_node_id = await self._read_type_definitions(object_or_variable_nodes)

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
                output.append(
                    OpcUaNodeInfo(
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
                )

            if fallback_entries:
                output.extend(await self._read_node_infos_fallback(fallback_entries))

        return output

    def _extract_class_node_info_attribute(
        self,
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
                self._endpoint,
                node_id,
                node_class.name,
                attribute.name,
                status_code,
            )
            return None
        if data_value.Value is None:
            return None
        return data_value.Value.Value

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
            await self._wait_for_reconnect(exc)
            retry_nodes = [self._client.get_node(node.nodeid) for node in nodes]
            values = await self._client.read_attributes(retry_nodes, attr=attr)
            self._record_read_data_values(values)
            return values

    async def _read_attribute_pairs(
        self,
        node_attributes: list[tuple[Any, AttributeIds]],
        batch_size: int | None = None,
    ) -> list[ua.DataValue]:
        if not node_attributes:
            return []

        if batch_size is None:
            limits = await self.get_operational_limits()
            batch_size = limits.max_nodes_per_read or len(node_attributes)

        batches = _chunked_nodes(node_attributes, max(1, batch_size))
        semaphore = asyncio.Semaphore(self._browse_concurrency)

        async def read_batch(batch: list[tuple[Any, AttributeIds]]) -> list[ua.DataValue]:
            async with semaphore:
                return await self._read_attribute_pairs_batch(batch)

        batch_values = await asyncio.gather(*(read_batch(batch) for batch in batches))
        return [value for values in batch_values for value in values]

    async def _read_attribute_pairs_batch(
        self,
        node_attributes: list[tuple[Any, AttributeIds]],
    ) -> list[ua.DataValue]:
        params = ua.ReadParameters()
        params.MaxAge = 0
        params.TimestampsToReturn = ua.TimestampsToReturn.Both
        params.NodesToRead = [
            ua.ReadValueId(NodeId=node.nodeid, AttributeId=int(attr)) for node, attr in node_attributes
        ]

        self._runtime_metrics.read_calls += 1
        self._runtime_metrics.read_nodes += len(node_attributes)
        try:
            values = await self._client.uaclient.read(params)
        except Exception as exc:
            if self._should_retry_after_disconnect(exc):
                logger.warning(
                    "OPC UA multi-attribute read retry after reconnect endpoint=%s batch_size=%d",
                    self._endpoint,
                    len(node_attributes),
                )
                await self._wait_for_reconnect(exc)
                values = await self._client.uaclient.read(params)
            elif self._is_too_many_operations_error(exc) and len(node_attributes) > 1:
                split_at = max(1, len(node_attributes) // 2)
                logger.warning(
                    "OPC UA multi-attribute read split retry endpoint=%s batch_size=%d split_at=%d",
                    self._endpoint,
                    len(node_attributes),
                    split_at,
                )
                left = await self._read_attribute_pairs_batch(node_attributes[:split_at])
                right = await self._read_attribute_pairs_batch(node_attributes[split_at:])
                return left + right
            else:
                self._record_failed_request()
                raise

        if len(values) != len(node_attributes):
            self._record_failed_request()
            raise RuntimeError(f"Read returned {len(values)} results for {len(node_attributes)} attributes")
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
        return await self._coalesce_metadata_load("operational_limits", self._load_operational_limits)

    async def _load_operational_limits(self) -> OpcUaOperationalLimits:
        started = perf_counter()
        max_nodes_per_browse: int | None = None
        max_nodes_per_read: int | None = None
        max_nodes_per_history_read_data: int | None = None

        try:
            operational_limits_node = self._client.get_node("i=11704")
            children = await operational_limits_node.get_children()
            browse_names = await self._read_attribute_batch_limited(
                children,
                AttributeIds.BrowseName,
                batch_size=max(1, len(children)),
            )
            limit_nodes: list[tuple[Any, str]] = []
            supported_limits = {
                "MaxNodesPerBrowse",
                "MaxNodesPerRead",
                "MaxNodesPerHistoryReadData",
            }
            for child, browse_name_value in zip(children, browse_names, strict=True):
                browse_name = getattr(self._extract_attribute_value(browse_name_value), "Name", None)
                if browse_name in supported_limits:
                    limit_nodes.append((child, browse_name))
            limit_values = await self._read_attribute_pairs(
                [(node, AttributeIds.Value) for node, _ in limit_nodes],
                batch_size=max(1, len(limit_nodes)),
            )
            for (_, browse_name), value_data in zip(limit_nodes, limit_values, strict=True):
                value = self._extract_attribute_value(value_data)
                if not isinstance(value, int) or value <= 0:
                    continue
                if browse_name == "MaxNodesPerBrowse":
                    max_nodes_per_browse = value
                elif browse_name == "MaxNodesPerRead":
                    max_nodes_per_read = value
                elif browse_name == "MaxNodesPerHistoryReadData":
                    max_nodes_per_history_read_data = value
        except Exception:
            logger.warning(
                "OPC UA operational limits read failed endpoint=%s; using internal defaults",
                self._endpoint,
                exc_info=True,
            )

        limits = OpcUaOperationalLimits(
            max_nodes_per_browse=max_nodes_per_browse,
            max_nodes_per_read=max_nodes_per_read,
            max_nodes_per_history_read_data=max_nodes_per_history_read_data,
        )
        self._limits_cache = limits
        logger.info(
            "OPC UA operational limits resolved endpoint=%s "
            "max_nodes_per_browse=%s max_nodes_per_read=%s max_nodes_per_history_read_data=%s duration_s=%.3f",
            self._endpoint,
            limits.max_nodes_per_browse,
            limits.max_nodes_per_read,
            limits.max_nodes_per_history_read_data,
            perf_counter() - started,
        )
        return limits

    async def get_subscription_capabilities(self) -> OpcUaSubscriptionCapabilities:
        if self._subscription_caps_cache is not None:
            return self._subscription_caps_cache
        return await self._coalesce_metadata_load("subscription_capabilities", self._load_subscription_capabilities)

    async def _load_subscription_capabilities(self) -> OpcUaSubscriptionCapabilities:
        started = perf_counter()

        node_ids = ["i=11714", "i=24096", "i=24097", "i=24098", "i=24104"]
        nodes = [self._client.get_node(node_id) for node_id in node_ids]
        try:
            capability_values = await self._read_attribute_pairs([(node, AttributeIds.Value) for node in nodes])
        except Exception:
            logger.debug("OPC UA subscription capability batch read failed endpoint=%s", self._endpoint, exc_info=True)
            fallback_values = await asyncio.gather(*(self._read_positive_int(node_id) for node_id in node_ids))
            values_by_node_id = dict(zip(node_ids, fallback_values, strict=True))
        else:
            values_by_node_id = {}
            for node, data_value in zip(nodes, capability_values, strict=True):
                value = self._extract_attribute_value(data_value)
                node_id = node.nodeid.to_string()
                values_by_node_id[node_id] = value if isinstance(value, int) and value > 0 else None

        capabilities = OpcUaSubscriptionCapabilities(
            max_monitored_items_per_call=values_by_node_id.get("i=11714"),
            max_subscriptions=values_by_node_id.get("i=24096"),
            max_monitored_items=values_by_node_id.get("i=24097"),
            max_subscriptions_per_session=values_by_node_id.get("i=24098"),
            max_monitored_items_per_subscription=values_by_node_id.get("i=24104"),
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

    async def _read_positive_int(self, node_id: str) -> int | None:
        try:
            value = await self._client.get_node(node_id).read_value()
        except Exception:
            logger.debug("OPC UA subscription capability read failed node_id=%s", node_id, exc_info=True)
            return None

        if isinstance(value, int) and value > 0:
            return value
        return None

    async def get_namespaces(self) -> list[str]:
        started = perf_counter()
        self._runtime_metrics.namespace_reads += 1
        try:
            raw = await self._client.nodes.namespace_array.read_value()
            namespaces = [str(item) for item in raw] if isinstance(raw, list) else []
            self._runtime_metrics.namespace_count_last = len(namespaces)
            logger.debug(
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
        return await self._get_cached_metadata(
            "namespace_infos", self._namespace_infos_cache, self._load_namespace_infos
        )

    async def _load_namespace_infos(self) -> list[OpcUaNamespaceInfo]:
        now = perf_counter()
        if self._namespace_infos_cache is not None:
            cached_at, cached_value = self._namespace_infos_cache
            if self._is_metadata_cache_entry_fresh(cached_at):
                logger.debug(
                    "OPC UA namespace info cache hit endpoint=%s age_s=%.3f",
                    self._endpoint,
                    now - cached_at,
                )
                return cached_value

        started = perf_counter()
        cold_build = self._runtime_metrics.namespace_info_builds == 0
        uris = await self.get_namespaces()
        server_uri: str | None = None
        try:
            server_array_node = self._client.get_node(f"i={ObjectIds.Server_ServerArray}")
            server_array = await server_array_node.read_value()
            if isinstance(server_array, (list, tuple)) and server_array:
                candidate = server_array[0]
                if isinstance(candidate, str) and candidate.strip():
                    server_uri = candidate.strip()
        except Exception:
            logger.warning(
                "OPC UA ServerArray read failed endpoint=%s; server URI unavailable",
                self._endpoint,
                exc_info=True,
            )

        display_by_uri: dict[str, str] = {}
        uri_by_component_id: dict[str, str] = {}
        version_by_component_id: dict[str, str] = {}

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
            logger.debug(
                "OPC UA namespace metadata components endpoint=%s count=%d",
                self._endpoint,
                len(namespace_components),
            )

            component_display_names = await asyncio.gather(
                *[component.read_display_name() for component in namespace_components],
                return_exceptions=True,
            )
            component_display_by_id: dict[str, str] = {}
            for component, display_name in zip(namespace_components, component_display_names, strict=True):
                if isinstance(display_name, BaseException):
                    logger.debug(
                        "OPC UA namespace metadata display name read failed endpoint=%s node_id=%s",
                        self._endpoint,
                        component.nodeid.to_string(),
                        exc_info=display_name,
                    )
                    continue
                display_text = getattr(display_name, "Text", None)
                component_display_by_id[component.nodeid.to_string()] = (
                    display_text if isinstance(display_text, str) else ""
                )

            component_children_browse = await self._browse_children_descriptions(
                namespace_components,
                max_nodes_per_browse,
            )

            metadata_failures = 0

            for component, refs in component_children_browse:
                component_display = component_display_by_id.get(component.nodeid.to_string(), "")
                logger.debug(
                    "OPC UA namespace metadata component endpoint=%s node_id=%s children=%d",
                    self._endpoint,
                    component.nodeid.to_string(),
                    len(refs),
                )

                for ref in refs:
                    if ref.BrowseName.Name not in {"NamespaceUri", "NamespaceVersion"}:
                        continue
                    child = self._client.get_node(ref.NodeId)
                    try:
                        value = await child.read_value()
                        if ref.BrowseName.Name == "NamespaceUri":
                            uri = str(value)
                            if uri:
                                display_by_uri[uri] = component_display or uri
                                uri_by_component_id[component.nodeid.to_string()] = uri
                        elif isinstance(value, str) and value.strip():
                            version_by_component_id[component.nodeid.to_string()] = value.strip()
                    except Exception as exc:
                        metadata_failures += 1
                        logger.debug(
                            "OPC UA namespace metadata NamespaceUri read failed endpoint=%s node_id=%s",
                            self._endpoint,
                            child.nodeid.to_string(),
                            exc_info=exc,
                        )
            if metadata_failures:
                logger.warning(
                    "OPC UA namespace metadata partially read endpoint=%s failures=%d; "
                    "using fallback names for missing entries",
                    self._endpoint,
                    metadata_failures,
                )
        except Exception:
            logger.warning(
                "OPC UA namespace metadata read from i=11715 failed endpoint=%s; using fallback names",
                self._endpoint,
            )

        resolved_versions = {
            uri: version_by_component_id[component_id]
            for component_id, uri in uri_by_component_id.items()
            if component_id in version_by_component_id
        }

        infos = [
            OpcUaNamespaceInfo(
                uri=uri,
                display_name=display_by_uri.get(uri, ""),
                namespace_version=resolved_versions.get(uri),
                server_uri=server_uri,
            )
            for uri in uris
        ]
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
        return await self._get_cached_metadata("object_types", self._object_types_cache, self._load_object_types)

    async def _load_object_types(self) -> list[OpcUaObjectTypeInfo]:
        now = perf_counter()
        if self._object_types_cache is not None:
            cached_at, cached_value = self._object_types_cache
            if self._is_metadata_cache_entry_fresh(cached_at):
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
                member_candidates: list[
                    tuple[str, str, str, str, NodeClass, Any, str | None, str | None, int | None]
                ] = []

                browsed = await self._browse_children_descriptions(nodes, max_nodes_per_browse)
                for parent_node, refs in browsed:
                    parent_node_id = parent_node.nodeid.to_string()
                    for ref_index, ref in enumerate(refs):
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

            logger.debug(
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

        data_type_by_node_id: dict[str, str | None] = {}
        data_type_batch_ok = True

        value_rank_by_node_id: dict[str, int | None] = {}
        value_rank_batch_ok = True
        member_attributes = [(node, AttributeIds.Description) for node in all_nodes]
        member_attributes.extend((node, AttributeIds.DataType) for node in variable_nodes)
        member_attributes.extend((node, AttributeIds.ValueRank) for node in variable_nodes)
        try:
            member_values = await self._read_attribute_pairs(member_attributes)
        except Exception:
            descriptions_batch_ok = False
            data_type_batch_ok = False
            value_rank_batch_ok = False
            logger.debug(
                "OPC UA member metadata batch read failed endpoint=%s",
                self._endpoint,
                exc_info=True,
            )
        else:
            offset = 0
            description_values = member_values[offset : offset + len(all_nodes)]
            for node_id, description_value in zip(all_node_ids, description_values, strict=True):
                description_raw = self._extract_attribute_value(description_value)
                text_value = getattr(description_raw, "Text", None)
                description_by_node_id[node_id] = (
                    text_value if isinstance(text_value, str) and text_value.strip() else None
                )
            offset += len(all_nodes)

            data_type_values = member_values[offset : offset + len(variable_nodes)]
            offset += len(variable_nodes)
            for node_id, data_type_value in zip(variable_node_ids, data_type_values, strict=True):
                data_type_raw = self._extract_attribute_value(data_type_value)
                if data_type_raw is not None:
                    data_type_by_node_id[node_id] = (
                        data_type_raw.to_string() if hasattr(data_type_raw, "to_string") else str(data_type_raw)
                    )

            value_rank_values = member_values[offset : offset + len(variable_nodes)]
            for node_id, value_rank_value in zip(variable_node_ids, value_rank_values, strict=True):
                value_rank_raw = self._extract_attribute_value(value_rank_value)
                if value_rank_raw is None:
                    continue
                if isinstance(value_rank_raw, int):
                    value_rank_by_node_id[node_id] = value_rank_raw
                else:
                    try:
                        value_rank_by_node_id[node_id] = int(value_rank_raw)
                    except (TypeError, ValueError):
                        value_rank_by_node_id[node_id] = None

        semaphore = asyncio.Semaphore(self._browse_concurrency)

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
                        data_type = await self._read_data_type_or_none(node)
                    type_definition_id = None
                else:
                    type_definition_id = await self._read_type_definition_id_or_none(node)

                data_value = await self._read_data_value_or_none(node) if node_class == NodeClass.Variable else None
                value = _variant_value_or_self(data_value)
                variant_type, is_array, array_dimensions = _variant_metadata(data_value)
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
                        array_dimensions=array_dimensions,
                        parent_node_id=parent_id,
                        type_definition_id=type_definition_id,
                        reference_type_id=reference_type_id,
                        reference_type=reference_type,
                        reference_order=reference_order,
                        modelling_rule=modelling_rule,
                    ),
                )

        return await asyncio.gather(
            *[
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

    async def _read_type_definition_id_or_none(self, node: Any) -> str | None:
        try:
            refs = await node.get_references(refs=ObjectIds.HasTypeDefinition, direction=ua.BrowseDirection.Forward)
            if not refs:
                return None
            return _normalize_type_definition_id(refs[0].NodeId.to_string())
        except Exception:
            logger.debug(
                "OPC UA type definition read failed endpoint=%s node_id=%s",
                self._endpoint,
                node.nodeid.to_string(),
                exc_info=True,
            )
            return None

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

        attributes = [(node, attr) for node in nodes for attr in (AttributeIds.Description, AttributeIds.IsAbstract)]
        values = await self._read_attribute_pairs(attributes)

        output: dict[str, dict[str, Any]] = {}
        for index, node in enumerate(nodes):
            node_id = node.nodeid.to_string()
            description_raw = self._extract_attribute_value(values[index * 2])
            abstract_raw = self._extract_attribute_value(values[index * 2 + 1])

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

            continuation_entries: list[tuple[Any, bytes, list[ua.ReferenceDescription]]] = []
            browse_next_references_total = 0
            for node, browse_result in zip(node_batch, browse_results, strict=True):
                refs = list(browse_result.References)
                continuation_point = browse_result.ContinuationPoint
                results.append((node, refs))
                if continuation_point:
                    continuation_entries.append((node, continuation_point, refs))

            while continuation_entries:
                next_params = ua.BrowseNextParameters()
                next_params.ReleaseContinuationPoints = False
                next_params.ContinuationPoints = [
                    continuation_point for _, continuation_point, _ in continuation_entries
                ]
                next_results = await self._browse_next_with_retry(
                    next_params=next_params,
                    node_ids=[node.nodeid.to_string() for node, _, _ in continuation_entries],
                )
                self._runtime_metrics.browse_next_calls += 1
                if len(next_results) != len(continuation_entries):
                    raise RuntimeError(
                        f"BrowseNext returned {len(next_results)} results for {len(continuation_entries)} points"
                    )

                next_entries: list[tuple[Any, bytes, list[ua.ReferenceDescription]]] = []
                for (node, _, refs), next_result in zip(continuation_entries, next_results, strict=True):
                    refs.extend(next_result.References)
                    browse_next_references_total += len(next_result.References)
                    self._runtime_metrics.browse_next_references += len(next_result.References)
                    if next_result.ContinuationPoint:
                        next_entries.append((node, next_result.ContinuationPoint, refs))
                continuation_entries = next_entries

            if browse_next_references_total:
                logger.debug(
                    "OPC UA browse-next endpoint=%s nodes=%d additional_references=%d",
                    self._endpoint,
                    len(browse_results),
                    browse_next_references_total,
                )

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
            await self._wait_for_reconnect(exc)
            results = await self._client.uaclient.browse(params)
            self._record_browse_results(results)
            return results

    async def _browse_next_with_retry(
        self,
        next_params: ua.BrowseNextParameters,
        node_ids: list[str],
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
                "OPC UA browse-next retry after reconnect endpoint=%s node_count=%d",
                self._endpoint,
                len(node_ids),
            )
            await self._wait_for_reconnect(exc)
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
                await self._wait_for_reconnect(exc)
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
                await self._wait_for_reconnect(exc)
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

        logger.debug(
            "OPC UA batch read ok endpoint=%s requested=%d batch_size=%d duration_s=%.3f",
            self._endpoint,
            len(node_ids),
            batch_size,
            perf_counter() - started,
        )
        return values

    async def read_data_values(self, node_ids: list[str]) -> list[ua.DataValue]:
        """Read Value attributes with one OPC UA Read service call per MaxNodesPerRead chunk.

        Per-node status codes are returned as-is. Transport failures propagate without
        per-node fan-out; other batch failures fall back to isolated per-node reads.
        """
        if not node_ids:
            return []

        started = perf_counter()
        try:
            nodes = [self._client.get_node(node_id) for node_id in node_ids]
            values = await self._read_attribute_batch_limited(nodes, AttributeIds.Value)
            if len(values) != len(node_ids):
                raise RuntimeError(f"Read returned {len(values)} results for {len(node_ids)} nodes")
        except Exception as exc:
            if self._is_transport_read_failure(exc):
                logger.warning(
                    "OPC UA batched data-value read unavailable endpoint=%s requested=%d; "
                    "not falling back to per-node reads",
                    self._endpoint,
                    len(node_ids),
                    exc_info=True,
                )
                raise
            logger.warning(
                "OPC UA batched data-value read failed endpoint=%s requested=%d; falling back to per-node reads",
                self._endpoint,
                len(node_ids),
                exc_info=True,
            )
            return await self._read_data_values_per_node(node_ids)

        logger.debug(
            "OPC UA batch data-value read ok endpoint=%s requested=%d duration_s=%.3f",
            self._endpoint,
            len(node_ids),
            perf_counter() - started,
        )
        return values

    async def _read_data_values_per_node(self, node_ids: list[str]) -> list[ua.DataValue]:
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
                        await self._wait_for_reconnect(exc)
                        try:
                            retry_value = await self._client.get_node(node_id).read_data_value()
                            self._record_read_data_values([retry_value])
                            return retry_value
                        except Exception as retry_exc:
                            if self._is_transport_read_failure(retry_exc):
                                logger.warning(
                                    "OPC UA data-value retry unavailable node_id=%s; aborting remaining reads",
                                    node_id,
                                    exc_info=True,
                                )
                                raise
                    elif self._is_transport_read_failure(exc):
                        logger.warning(
                            "OPC UA data-value read unavailable node_id=%s; aborting remaining reads",
                            node_id,
                            exc_info=True,
                        )
                        raise
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
            tasks = [asyncio.create_task(_read_one(nid)) for nid in chunk]
            try:
                chunk_results = await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            results.extend(chunk_results)

        self._runtime_metrics.read_calls += 1
        self._runtime_metrics.read_nodes += len(node_ids)
        logger.debug(
            "OPC UA batch data-value read ok endpoint=%s requested=%d duration_s=%.3f",
            self._endpoint,
            len(node_ids),
            perf_counter() - started,
        )
        return results

    async def read_write_access(self, node_id: str) -> tuple[bool, bool]:
        node = self._client.get_node(node_id)
        try:
            values = await node.read_attributes([AttributeIds.AccessLevel, AttributeIds.UserAccessLevel])
            access_level = _attribute_scalar_value(values[0]) if len(values) > 0 else None
            user_access_level = _attribute_scalar_value(values[1]) if len(values) > 1 else None
            return _is_write_access_allowed(access_level), _is_write_access_allowed(user_access_level)
        except Exception as exc:
            if self._should_retry_after_disconnect(exc):
                logger.warning(
                    "OPC UA write access read retry after reconnect node_id=%s endpoint=%s",
                    node_id,
                    self._endpoint,
                )
                await self._wait_for_reconnect(exc)
                retry_node = self._client.get_node(node_id)
                retry_values = await retry_node.read_attributes(
                    [AttributeIds.AccessLevel, AttributeIds.UserAccessLevel]
                )
                access_level = _attribute_scalar_value(retry_values[0]) if len(retry_values) > 0 else None
                user_access_level = _attribute_scalar_value(retry_values[1]) if len(retry_values) > 1 else None
                return _is_write_access_allowed(access_level), _is_write_access_allowed(user_access_level)
            self._record_failed_request()
            raise

    async def read_history_write_access(self, node_id: str) -> tuple[bool, bool]:
        node = self._client.get_node(node_id)
        try:
            values = await node.read_attributes([AttributeIds.AccessLevel, AttributeIds.UserAccessLevel])
            if len(values) != 2:
                raise UaError("Incomplete OPC UA history access response")
            for item in values:
                if item.StatusCode is None:
                    raise UaError("Missing OPC UA history access status")
                item.StatusCode.check()
            mask = 1 << ua.AccessLevel.HistoryWrite
            return (
                bool(int(_attribute_scalar_value(values[0])) & mask),
                bool(int(_attribute_scalar_value(values[1])) & mask),
            )
        except Exception:
            self._record_failed_request()
            raise

    def history_update_supported(self) -> bool | None:
        return self._history_update_supported

    async def write_history_value(self, node_id: str, value: Any, quality: str, timestamp: datetime) -> None:
        node = self._client.get_node(node_id)
        try:
            variant_type = await node.read_data_type_as_variant_type()
            attributes = await node.read_attributes([AttributeIds.ValueRank, AttributeIds.ArrayDimensions])
            if len(attributes) != 2:
                raise UaError("Incomplete OPC UA history type response")
            for attribute in attributes:
                if attribute.StatusCode is None:
                    raise UaError("Missing OPC UA history type status")
            rank_attribute, dimensions_attribute = attributes
            assert rank_attribute.StatusCode is not None
            assert dimensions_attribute.StatusCode is not None
            rank_attribute.StatusCode.check()
            dimensions = None
            if dimensions_attribute.StatusCode.value != StatusCodes.BadAttributeIdInvalid:
                dimensions_attribute.StatusCode.check()
                dimensions = _attribute_scalar_value(dimensions_attribute)
            data_value = historical_data_value(
                value,
                quality,
                timestamp,
                variant_type,
                int(_attribute_scalar_value(rank_attribute)),
                dimensions,
            )
            details = ua.UpdateDataDetails(
                NodeId=node.nodeid,
                PerformInsertReplace=ua.PerformUpdateType.Update,
                UpdateValues=[data_value],
            )
            # Do not replay a mutation after a timeout: the server may already have applied it.
            result = await node.history_update(details)
            result.StatusCode.check()
            if len(result.OperationResults) != 1:
                raise UaError("Incomplete OPC UA HistoryUpdate operation results")
            result.OperationResults[0].check()
        except Exception as exc:
            if isinstance(exc, UaStatusCodeError) and exc.code == StatusCodes.BadServiceUnsupported:
                self._history_update_supported = False
            self._record_failed_request()
            raise
        self._history_update_supported = True
        self._request_metrics.history_write_count += 1
        logger.info("OPC UA history update ok node_id=%s timestamp=%s", node_id, timestamp.isoformat())

    async def read_variant_type(self, node_id: str) -> str | None:
        node = self._client.get_node(node_id)
        try:
            variant_type = await node.read_data_type_as_variant_type()
            return str(getattr(variant_type, "name", variant_type)) if variant_type is not None else None
        except Exception as exc:
            if self._should_retry_after_disconnect(exc):
                logger.warning(
                    "OPC UA variant type read retry after reconnect node_id=%s endpoint=%s",
                    node_id,
                    self._endpoint,
                )
                await self._wait_for_reconnect(exc)
                retry_node = self._client.get_node(node_id)
                retry_variant_type = await retry_node.read_data_type_as_variant_type()
                return (
                    str(getattr(retry_variant_type, "name", retry_variant_type))
                    if retry_variant_type is not None
                    else None
                )
            return None

    async def write_value(
        self,
        node_id: str,
        value: Any,
        variant_type: str | None,
        quality: str,
        timestamp: datetime,
    ) -> None:
        node = self._client.get_node(node_id)
        explicit_variant = _to_explicit_ua_variant(value, variant_type)
        payload = ua.DataValue(
            Value=explicit_variant if explicit_variant is not None else ua.Variant(value),
            StatusCode=quality_status_code(quality),
            SourceTimestamp=ua.DateTime.fromisoformat(timestamp.astimezone(timezone.utc).isoformat()),
        )
        try:
            # Do not replay a write after a connection loss: the server may already have applied it.
            await node.write_value(payload)
            self._request_metrics.write_count += 1
        except Exception:
            self._record_failed_request()
            raise

        try:
            written_data_value = await node.read_data_value(raise_on_bad_status=False)
        except Exception as exc:
            self._record_failed_request()
            raise OpcUaWriteMetadataVerificationError(
                "OPC UA server accepted the current-value write, but the requested quality and source timestamp "
                "could not be verified; write outcome is uncertain"
            ) from exc
        actual_quality = _quality_from_status_code(written_data_value.StatusCode)
        actual_timestamp = written_data_value.SourceTimestamp
        expected_timestamp = timestamp.astimezone(timezone.utc)
        if actual_quality != quality or actual_timestamp != expected_timestamp:
            self._record_failed_request()
            raise OpcUaWriteMetadataUnsupportedError(
                "OPC UA server did not preserve the requested quality and source timestamp; "
                "current-value VQT writes are unsupported by this server"
            )

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
                await self._wait_for_reconnect(exc)
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
        self._runtime_metrics.history_read_nodes += len(node_ids)
        limits = await self.get_operational_limits()
        batch_size = limits.max_nodes_per_history_read_data or len(node_ids)
        details = ua.ReadRawModifiedDetails(
            IsReadModified=False,
            StartTime=start_time if start_time is not None else ua.get_win_epoch(),
            EndTime=end_time if end_time is not None else ua.get_win_epoch(),
            NumValuesPerNode=0,
            ReturnBounds=False,
        )
        values_by_node_id: dict[str, list[ua.DataValue]] = {}
        history_batches = _chunked(node_ids, max(1, batch_size))
        semaphore = asyncio.Semaphore(self._browse_concurrency)

        async def read_history_batch(node_batch: list[str]) -> dict[str, list[ua.DataValue]]:
            async with semaphore:
                batch_values = await self._read_history_batch(details, node_batch)
                batch_values_by_node_id: dict[str, list[ua.DataValue]] = {node_id: [] for node_id in node_batch}
                active_node_ids = list(node_batch)
                active_results = batch_values

                while active_node_ids:
                    next_node_ids: list[str] = []
                    next_continuations: dict[str, bytes] = {}
                    for node_id, result in zip(active_node_ids, active_results, strict=True):
                        try:
                            result.StatusCode.check()
                        except Exception as exc:
                            if self._is_history_missing_or_unsupported(exc):
                                logger.info(
                                    "OPC UA history read returned no data or unsupported history "
                                    "endpoint=%s node_id=%s",
                                    self._endpoint,
                                    node_id,
                                )
                                batch_values_by_node_id[node_id] = []
                                continue
                            raise

                        history_data = result.HistoryData
                        data_values = getattr(history_data, "DataValues", None)
                        if not isinstance(data_values, list):
                            raise UaError(f"Incomplete OPC UA history response for {node_id}")
                        batch_values_by_node_id[node_id].extend(data_values)
                        continuation_point = result.ContinuationPoint
                        if continuation_point:
                            next_node_ids.append(node_id)
                            next_continuations[node_id] = continuation_point

                    if not next_node_ids:
                        break
                    active_results = await self._read_history_batch(
                        details,
                        next_node_ids,
                        [next_continuations[node_id] for node_id in next_node_ids],
                    )
                    active_node_ids = next_node_ids
                return batch_values_by_node_id

        try:
            batch_results = await asyncio.gather(*(read_history_batch(batch) for batch in history_batches))
            for batch_values in batch_results:
                values_by_node_id.update(batch_values)
        except Exception:
            self._record_failed_request()
            raise

        for _ in range(len(values_by_node_id)):
            self._record_history_read_success()

        logger.debug(
            "OPC UA history read ok endpoint=%s nodes=%d values=%d duration_s=%.3f",
            self._endpoint,
            len(node_ids),
            sum(len(item) for item in values_by_node_id.values()),
            perf_counter() - started,
        )
        return values_by_node_id

    async def _read_history_batch(
        self,
        details: ua.ReadRawModifiedDetails,
        node_ids: list[str],
        continuation_points: list[bytes | None] | None = None,
    ) -> list[ua.HistoryReadResult]:
        points = continuation_points or [None] * len(node_ids)
        params = ua.HistoryReadParameters()
        params.HistoryReadDetails = details
        params.TimestampsToReturn = ua.TimestampsToReturn.Both
        params.ReleaseContinuationPoints = False
        params.NodesToRead = [
            ua.HistoryReadValueId(NodeId=self._client.get_node(node_id).nodeid, ContinuationPoint=point)
            for node_id, point in zip(node_ids, points, strict=True)
        ]

        self._runtime_metrics.history_read_calls += 1
        try:
            results = await self._client.uaclient.history_read(params)
        except Exception as exc:
            if self._should_retry_after_disconnect(exc):
                logger.warning(
                    "OPC UA batched history read retry after reconnect endpoint=%s node_count=%d",
                    self._endpoint,
                    len(node_ids),
                )
                await self._wait_for_reconnect(exc)
                results = await self._client.uaclient.history_read(params)
            elif self._is_too_many_operations_error(exc) and len(node_ids) > 1:
                split_at = max(1, len(node_ids) // 2)
                logger.warning(
                    "OPC UA history read split retry endpoint=%s batch_size=%d split_at=%d",
                    self._endpoint,
                    len(node_ids),
                    split_at,
                )
                left = await self._read_history_batch(details, node_ids[:split_at], points[:split_at])
                right = await self._read_history_batch(details, node_ids[split_at:], points[split_at:])
                return left + right
            else:
                raise

        if len(results) != len(node_ids):
            raise UaError(f"HistoryRead returned {len(results)} results for {len(node_ids)} nodes")
        return results

    def _is_history_missing_or_unsupported(self, exc: Exception) -> bool:
        lowered = str(exc).lower()
        return any(
            token in lowered
            for token in (
                "badhistoryoperationunsupported",
                "historyoperationunsupported",
                "historyread not supported",
                "history read not supported",
                "badnodata",
                "goodnodata",
                "no data exists for the requested time range",
            )
        )

    async def call_method(self, object_node_id: str, method_node_id: str, args: list[Any]) -> Any:
        started = perf_counter()
        self._runtime_metrics.method_calls += 1
        object_node = self._client.get_node(object_node_id)
        try:
            # Do not replay a method call after a connection loss: it may already have run.
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
        except Exception:
            self._record_failed_request()
            logger.exception(
                "OPC UA method failed object_node_id=%s method_node_id=%s arg_count=%d duration_s=%.3f",
                object_node_id,
                method_node_id,
                len(args),
                perf_counter() - started,
            )
            raise

    async def _wait_for_reconnect(self, exc: Exception) -> None:
        """Wait until asyncua's supervisor has re-established the session after `exc`.

        Raises ConnectionError if the client is not connected again within the reconnect
        request timeout, so callers fail fast instead of piling up behind a dead connection.
        """
        uaclient = self._client.uaclient
        if (
            uaclient.state is UaClientState.CONNECTED
            and isinstance(exc, UaStatusCodeError)
            and exc.code in _CONNECTION_LOST_STATUS_CODES
        ):
            # The transport is up but the server dropped the session/channel; asyncua's
            # watchdog would only notice on its next tick, so hand the loss over right away.
            logger.warning(
                "OPC UA session lost endpoint=%s status=%s; triggering asyncua reconnect",
                self._endpoint,
                exc,
            )
            uaclient.notify_transport_lost()
        timeout = self._reconnect_request_timeout_seconds
        async with uaclient.subscribe_state() as states:
            try:
                await states.wait_for_state(UaClientState.CONNECTED, timeout=timeout)
            except asyncio.TimeoutError:
                raise ConnectionError(
                    f"OPC UA client did not reconnect within {timeout:.1f}s (state={uaclient.state.value})"
                ) from exc

    def _should_retry_after_disconnect(self, exc: Exception) -> bool:
        """True for errors caused by a lost connection or session, never for request timeouts."""
        if isinstance(exc, UaStatusCodeError):
            return exc.code in _CONNECTION_LOST_STATUS_CODES
        if isinstance(exc, TimeoutError):
            return False
        text = str(exc).lower()
        if "waiting for client to reconnect" in text:
            # asyncua's request gate already waited for the reconnect; do not wait twice.
            return False
        if isinstance(exc, ConnectionError):
            return True
        return any(marker in text for marker in _CONNECTION_LOST_MARKERS)

    def _is_transport_read_failure(self, exc: Exception) -> bool:
        return (
            isinstance(exc, (TimeoutError, ConnectionError))
            or self._should_retry_after_disconnect(exc)
            or (
                isinstance(exc, UaStatusCodeError)
                and exc.code in {StatusCodes.BadTimeout, StatusCodes.BadRequestTimeout, StatusCodes.BadServerTooBusy}
            )
        )

    def _is_too_many_operations_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return "badtoomanyoperations" in text or "too many operations" in text

    def add_reconnect_listener(self, listener: Callable[[], Awaitable[None]]) -> None:
        self._reconnect_listeners.append(listener)

    def remove_reconnect_listener(self, listener: Callable[[], Awaitable[None]]) -> None:
        with suppress(ValueError):
            self._reconnect_listeners.remove(listener)

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

    async def unsubscribe_data_changes(self, subscription: Any, handles: list[int]) -> None:
        await subscription.unsubscribe(handles)

    async def delete_subscription(self, subscription: Any) -> None:
        await subscription.delete()

    def get_monitored_item_handles(self, subscription: Any, node_ids: list[str]) -> dict[str, int] | None:
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
            handle = handles_by_node_id.get(self._client.get_node(node_id).nodeid)
            if handle is not None:
                result[node_id] = handle
        return result

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


def _to_explicit_ua_variant(value: Any, variant_type: str | None) -> ua.Variant | None:
    if value is None:
        return ua.Variant(None, ua.VariantType.Null)
    if variant_type is None:
        return None
    normalized = variant_type.strip()
    if not normalized:
        return None
    ua_variant_type = getattr(ua.VariantType, normalized, None)
    if ua_variant_type is None:
        return None
    return ua.Variant(value, ua_variant_type)


def _quality_from_status_code(status_code: ua.StatusCode | None) -> str | None:
    if status_code is None:
        return None
    code = int(status_code.value)
    if code == int(StatusCodes.GoodNoData):
        return "GoodNoData"
    severity = code & 0xC0000000
    if severity == 0:
        return "Good"
    if severity == 0x40000000:
        return "Uncertain"
    if severity == 0x80000000:
        return "Bad"
    return None


def _attribute_scalar_value(data_value: Any) -> Any:
    if data_value is None:
        return None
    variant = getattr(data_value, "Value", None)
    if variant is None:
        return data_value
    return getattr(variant, "Value", variant)


def _coerce_access_level(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_write_access_allowed(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, set):
        labels = {str(item).lower() for item in value}
        return "currentwrite" in labels or "write" in labels
    raw = getattr(value, "value", value)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        return bool(raw & 0x02)
    try:
        numeric = int(raw)
    except (TypeError, ValueError):
        return "write" in str(raw).lower()
    return bool(numeric & 0x02)
