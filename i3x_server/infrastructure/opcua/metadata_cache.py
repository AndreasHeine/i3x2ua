"""Metadata cache mechanics and coalesced metadata loads for the OPC UA adapter.

The TTL/freshness rules, the node-id keyed dict-cache primitives, and the
in-flight load coalescing live here. Functions take the owning ``OpcUaClient`` and
reach it through public seams only; ``client.py`` keeps thin delegating methods so
the class stays the entry point for the seam modules and tests. The dict caches
themselves stay owned by the client and are passed in explicitly, so this module
never touches client internals.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import TYPE_CHECKING, Any, TypeVar, cast

if TYPE_CHECKING:
    from i3x_server.infrastructure.opcua.client import OpcUaClient

logger = logging.getLogger(__name__)

_T = TypeVar("_T")
_METADATA_INVALIDATED_AT = float("-inf")

# Caps for the node-id keyed metadata caches below. Entries only expire on read, so without
# a cap a long-lived client accumulates every type it ever browsed. The caps are generous so
# hot base types stay resident in normal deployments and eviction only bounds pathological
# growth.
_TYPE_DEFINITION_HIERARCHY_CACHE_MAX_ENTRIES = 2048
_REFERENCE_TYPE_SUPERTYPES_CACHE_MAX_ENTRIES = 1024


def is_metadata_cache_entry_fresh(client: OpcUaClient, cached_at: float) -> bool:
    """TTL of 0 means entries never expire, unless explicitly invalidated."""
    if cached_at == _METADATA_INVALIDATED_AT:
        return False
    if client.metadata_cache_ttl_seconds == 0:
        return True
    return perf_counter() - cached_at <= client.metadata_cache_ttl_seconds


def invalidate_metadata_entry(cached: tuple[float, _T] | None) -> tuple[float, _T] | None:
    """Keep the value servable as stale, but force the next access to refresh it."""
    if cached is None:
        return None
    return (_METADATA_INVALIDATED_AT, cached[1])


def _metadata_dict_cache_read(client: OpcUaClient, entries: dict[str, tuple[float, _T]], key: str) -> _T | None:
    """Return the fresh value for ``key`` and mark the entry most-recently-used.

    Stale entries are dropped rather than kept: dict-keyed metadata is never served as
    stale, so unreachable entries would only delay eviction.
    """
    entry = entries.get(key)
    if entry is None:
        return None
    if not client.is_metadata_cache_entry_fresh(entry[0]):
        del entries[key]
        return None
    entries[key] = entries.pop(key)
    return entry[1]


def _metadata_dict_cache_write(
    client: OpcUaClient, entries: dict[str, tuple[float, _T]], key: str, value: _T, max_entries: int
) -> None:
    """Store an entry as most-recently-used and evict least-recently-used ones past the cap."""
    entries[key] = (perf_counter(), value)
    while len(entries) > max_entries:
        del entries[next(iter(entries))]


def _start_metadata_load(client: OpcUaClient, key: str, loader: Callable[[], Awaitable[_T]]) -> asyncio.Future[_T]:
    task = client.metadata_load_tasks.get(key)
    if task is None:
        task = asyncio.ensure_future(loader())
        client.metadata_load_tasks[key] = task
        task.add_done_callback(lambda done: _finish_metadata_load(client, key, done))
    return cast("asyncio.Future[_T]", task)


async def _coalesce_metadata_load(client: OpcUaClient, key: str, loader: Callable[[], Awaitable[_T]]) -> _T:
    """Share one in-flight OPC UA metadata load between concurrent callers.

    Without this, every request that sees an expired cache entry starts its own full
    browse, which overloads the OPC UA server under concurrent load.
    """
    return await asyncio.shield(_start_metadata_load(client, key, loader))


async def _get_cached_metadata(
    client: OpcUaClient,
    key: str,
    cached: tuple[float, _T] | None,
    loader: Callable[[], Awaitable[_T]],
) -> _T:
    """Serve stale metadata while one shared background load refreshes it.

    Only a cold cache waits for OPC UA, so TTL expiry never stalls API requests.
    """
    if cached is None:
        return await _coalesce_metadata_load(client, key, loader)
    cached_at, cached_value = cached
    if not client.is_metadata_cache_entry_fresh(cached_at):
        incident = client.stale_metadata_incidents.get(key)
        if incident is None:
            client.stale_metadata_incidents[key] = (perf_counter(), 0)
            logger.warning(
                "OPC UA serving stale metadata while refreshing endpoint=%s key=%s age_s=%s ttl_s=%s",
                client.endpoint,
                key,
                "invalidated" if cached_at == _METADATA_INVALIDATED_AT else f"{perf_counter() - cached_at:.3f}",
                client.metadata_cache_ttl_seconds,
            )
        else:
            started_at, suppressed_events = incident
            client.stale_metadata_incidents[key] = (started_at, suppressed_events + 1)
        _start_metadata_load(client, key, loader)
    return cached_value


def _finish_metadata_load(client: OpcUaClient, key: str, task: asyncio.Future[Any]) -> None:
    if client.metadata_load_tasks.get(key) is task:
        del client.metadata_load_tasks[key]
    if task.cancelled() or task.exception() is not None:
        return

    incident = client.stale_metadata_incidents.pop(key, None)
    if incident is not None:
        started_at, suppressed_events = incident
        logger.info(
            "OPC UA metadata refresh recovered endpoint=%s key=%s stale_duration_s=%.3f suppressed_events=%d",
            client.endpoint,
            key,
            perf_counter() - started_at,
            suppressed_events,
        )
