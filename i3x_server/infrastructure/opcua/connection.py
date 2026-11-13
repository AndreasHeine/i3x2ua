"""Connection lifecycle for the OPC UA adapter: connect/disconnect, state mirroring,
reconnect hand-off, and security setup.

Functions take the owning ``OpcUaClient`` and reach it through public seams only;
``client.py`` keeps thin delegating methods so the class stays the entry point for
the supervisor callbacks, tests, and the app factory. The asyncua supervisor owns
the reconnect mechanics (``recovery._SupervisedAsyncuaClient``); this module owns
the gateway-facing state snapshot and the follow-up work after a reconnect.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING

from asyncua.client.ua_client import UaClientState

from i3x_server.domain.ports.opcua import SecurityMode

if TYPE_CHECKING:
    from i3x_server.infrastructure.opcua.client import OpcUaClient

logger = logging.getLogger(__name__)


async def connect(client: OpcUaClient) -> None:
    started = perf_counter()
    security_started = perf_counter()
    await client.configure_security_if_needed()
    security_duration_s = perf_counter() - security_started
    logger.info(
        "OPC UA connect started endpoint=%s auth_mode=%s security_mode=%s",
        client.endpoint,
        "userpass" if client.using_user_auth else "anonymous",
        client.security_mode,
    )
    session_started = perf_counter()
    # Client.disconnect() clears asyncua's request gate and never restores it on connect().
    client.raw_client.uaclient.pre_request_hook = client.raw_client._wait_until_ready
    await client.raw_client.connect(
        auto_reconnect=True,
        reconnect_max_delay=client.reconnect_max_delay_seconds,
        reconnect_request_timeout=client.reconnect_request_timeout_seconds,
    )
    client.set_connection_state("Connected")
    client.start_connection_tasks()
    session_duration_s = perf_counter() - session_started
    typedef_started = perf_counter()
    await client.load_additional_typedefinitions()
    typedef_duration_s = perf_counter() - typedef_started
    limits_started = perf_counter()
    client.limits_cache = await client.get_operational_limits()
    limits_duration_s = perf_counter() - limits_started
    logger.info(
        "OPC UA limits endpoint=%s max_nodes_per_browse=%s max_nodes_per_read=%s",
        client.endpoint,
        client.limits_cache.max_nodes_per_browse,
        client.limits_cache.max_nodes_per_read,
    )
    logger.info(
        ("OPC UA connect phases endpoint=%s security_s=%.3f session_s=%.3f typedef_s=%.3f limits_s=%.3f total_s=%.3f"),
        client.endpoint,
        security_duration_s,
        session_duration_s,
        typedef_duration_s,
        limits_duration_s,
        perf_counter() - started,
    )


async def load_additional_typedefinitions(client: OpcUaClient) -> None:
    started = perf_counter()
    logger.info("OPC UA additional type definitions load started endpoint=%s", client.endpoint)
    try:
        await client.raw_client.load_data_type_definitions()
    except Exception as e:
        logger.warning(
            "OPC UA additional v1.04 data type definitions load failed endpoint=%s error=%s", client.endpoint, e
        )
        try:
            await client.raw_client.load_type_definitions()
        except Exception as e:
            logger.warning(
                "OPC UA additional v1.03 data type definitions load failed endpoint=%s error=%s", client.endpoint, e
            )
        else:
            logger.info(
                "OPC UA additional type definitions v1.03 load finished endpoint=%s duration_s=%.3f",
                client.endpoint,
                perf_counter() - started,
            )
    else:
        logger.info(
            "OPC UA additional type definitions load finished endpoint=%s duration_s=%.3f",
            client.endpoint,
            perf_counter() - started,
        )


async def disconnect(client: OpcUaClient) -> None:
    started = perf_counter()
    logger.info("OPC UA disconnect started endpoint=%s", client.endpoint)
    await client.stop_connection_tasks()
    # Drop reconnect bookkeeping: a later `connect()` must not run the reconnect
    # handler for a stale event or re-fire listeners with a recreated-session flag (L10).
    client.reconnected_event.clear()
    client.reconnect_session_recreated = False
    await client.raw_client.disconnect()
    client.set_connection_state("Disconnected")
    client.limits_cache = None
    client.namespace_infos_cache = None
    client.object_type_cache = None
    client.subscription_caps_cache = None
    client.reset_metadata_dict_caches()
    logger.info("OPC UA disconnect finished endpoint=%s duration_s=%.3f", client.endpoint, perf_counter() - started)


def start_connection_tasks(client: OpcUaClient) -> None:
    if client.connection_state_task is None or client.connection_state_task.done():
        client.connection_state_task = asyncio.create_task(client.watch_connection_state())
    if client.reconnect_handler_task is None or client.reconnect_handler_task.done():
        client.reconnect_handler_task = asyncio.create_task(client.reconnect_handler_loop())


async def stop_connection_tasks(client: OpcUaClient) -> None:
    tasks = [client.connection_state_task, client.reconnect_handler_task]
    client.connection_state_task = None
    client.reconnect_handler_task = None
    for task in tasks:
        if task is None:
            continue
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def watch_connection_state(client: OpcUaClient) -> None:
    """Mirror asyncua's transport state into the connection snapshot."""
    uaclient = client.raw_client.uaclient
    async with uaclient.subscribe_state() as states:
        client.apply_client_state(uaclient.state)
        while True:
            client.apply_client_state(await states.next_change())


def apply_client_state(client: OpcUaClient, state: UaClientState) -> None:
    if state is UaClientState.CONNECTED:
        client.set_connection_state("Connected")
        return
    if client.raw_client.uaclient.is_disconnect_requested:
        client.set_connection_state("Disconnected")
        return
    if client.connection_state == "Connected":
        logger.warning(
            "OPC UA connection lost endpoint=%s client_state=%s; waiting for asyncua reconnect",
            client.endpoint,
            state.value,
        )
    client.set_connection_state("Reconnecting")


def on_asyncua_reconnected(client: OpcUaClient, session_reused: bool) -> None:
    """Called by the asyncua supervisor once the session and its subscriptions are restored."""
    client.set_connection_state("Connected")
    client.limits_cache = None
    client.subscription_caps_cache = None
    # Keep the expensive browse results servable as stale so API requests do not
    # block on a cold rebuild while the server is recovering from the reconnect.
    client.namespace_infos_cache = client.invalidate_metadata_entry(client.namespace_infos_cache)
    client.object_type_cache = client.invalidate_metadata_entry(client.object_type_cache)
    client.reset_metadata_dict_caches()
    client.reconnect_session_recreated = client.reconnect_session_recreated or not session_reused
    logger.info(
        "OPC UA reconnect finished endpoint=%s session=%s",
        client.endpoint,
        "reused" if session_reused else "recreated",
    )
    # The follow-up work runs in our own task: the supervisor must not block on it, and
    # requests issued from the supervisor's context would bypass asyncua's request gate.
    client.reconnected_event.set()


async def reconnect_handler_loop(client: OpcUaClient) -> None:
    while True:
        await client.reconnected_event.wait()
        client.reconnected_event.clear()
        await client.handle_reconnected()


async def handle_reconnected(client: OpcUaClient) -> None:
    if client.reconnect_session_recreated:
        client.reconnect_session_recreated = False
        await client.load_additional_typedefinitions()
    for listener in list(client.reconnect_listeners):
        try:
            await listener()
        except Exception:
            logger.exception("OPC UA reconnect listener failed endpoint=%s", client.endpoint)


async def configure_security_if_needed(client: OpcUaClient) -> None:
    mode = client.security_mode.strip()
    # Env values are lenient (``none``, ``NONE``); only the canonical mode means "no security".
    if mode.lower() == SecurityMode.NONE.lower():
        client.using_security = False
        return

    missing: list[str] = []
    if client.security_policy is None:
        missing.append("security_policy")
    if client.client_cert_path is None:
        missing.append("client_cert_path")
    if client.client_key_path is None:
        missing.append("client_key_path")
    if missing:
        raise ValueError(
            f"OPC UA encryption requires policy, client cert, and client key. Missing: {', '.join(missing)}"
        )

    cert_path_str = client.client_cert_path
    key_path_str = client.client_key_path
    if cert_path_str is None or key_path_str is None:
        raise ValueError("OPC UA encryption paths are not configured")

    cert_path = Path(cert_path_str)
    key_path = Path(key_path_str)
    _assert_file_exists(cert_path, "OPC UA client certificate")
    _assert_file_exists(key_path, "OPC UA client key")

    key_value = str(key_path)
    if client.client_key_password:
        key_value = f"{key_value}::{client.client_key_password}"

    security_policy = client.security_policy
    if security_policy is None:
        raise ValueError("OPC UA security policy is not configured")

    security_items: list[str] = [security_policy, mode, str(cert_path), key_value]
    if client.server_cert_path is not None:
        server_cert = Path(client.server_cert_path)
        _assert_file_exists(server_cert, "OPC UA server certificate")
        security_items.append(str(server_cert))

    security_string = ",".join(security_items)
    await client.raw_client.set_security_string(security_string)
    client.using_security = True


def set_connection_state(client: OpcUaClient, state: str) -> None:
    if client.connection_state == state:
        return
    client.connection_state = state
    client.connection_state_since = datetime.now(tz=timezone.utc)


def _assert_file_exists(path: Path, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")
