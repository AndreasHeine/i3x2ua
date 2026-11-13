"""Typed application state container installed by the composition root.

``GatewayState`` replaces the stringly ``app.state`` bag: every runtime collaborator is a
declared field, so a missing field is an ``AttributeError`` instead of a silent
``getattr(..., default)`` fallback, and mypy can check the access paths. The container lives
in the application layer because route handlers and application dependencies read it; the
composition root (``bootstrap``) owns construction and lifecycle assignment. ``app.state``
stays Starlette's ``State`` slot - ``install_gateway_state`` wires the container into it.

Ownership of the field types follows the architecture document: the OPC UA client and the
subscription service are reached through their ports, the model builder and ``BuildResult``
are domain-side collaborators, and the MCP fields stay opaque because the MCP adapter owns
their shapes.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

from fastapi import FastAPI, Request
from starlette.datastructures import State

from i3x_server.application.ports.subscription import SubscriptionServicePort
from i3x_server.application.services.model_query import NamespaceSnapshotCache
from i3x_server.domain.ports.opcua import OpcUaClientProtocol
from i3x_server.model.builder import ModelBuilder
from i3x_server.prompts.registry import PromptRegistry
from i3x_server.schemas.state import BuildResult

__all__ = [
    "GatewayState",
    "McpTransportSession",
    "ObjectTypeContextCache",
    "ObjectTypeContextSlot",
    "UaStatusSnapshot",
    "gateway_state",
    "install_gateway_state",
]


@dataclass(slots=True)
class UaStatusSnapshot:
    """Last successful ServerStatus read plus when it was taken and on which connection."""

    payload: Any
    connection_state: str
    fetched_at: float


@dataclass(slots=True)
class McpTransportSession:
    """Legacy MCP transport session: protocol version plus its live SSE streams.

    ``protocol_version`` is supplied by the MCP adapter, which owns the protocol constants.
    """

    protocol_version: str
    streams: set[asyncio.Queue[dict[str, Any] | None]] = field(default_factory=set)
    last_used: float = field(default_factory=time.monotonic)


@dataclass(slots=True)
class ObjectTypeContextSlot:
    """One cached object-type context and the token triple that identifies it."""

    model_token: str
    namespace_token: tuple[Any, ...]
    object_types_token: str
    context: Any


@dataclass(slots=True)
class ObjectTypeContextCache:
    """Bounded most-recently-used cache of built object-type contexts.

    ``context`` is the presentation-owned context object; the container keeps it opaque so
    the application layer does not depend on protocol helpers. Entries are ordered
    least-recently-used first, so ``entries[-1]`` is the current context.
    """

    max_entries: int
    entries: list[ObjectTypeContextSlot] = field(default_factory=list)

    def current_slot(self) -> ObjectTypeContextSlot | None:
        return self.entries[-1] if self.entries else None

    def find(self, tokens: tuple[str, tuple[Any, ...], str]) -> ObjectTypeContextSlot | None:
        """Return the slot matching the token triple, searching from the most recent entry."""
        for slot in reversed(self.entries):
            if (slot.model_token, slot.namespace_token, slot.object_types_token) == tokens:
                return slot
        return None

    def promote(self, slot: ObjectTypeContextSlot) -> None:
        """Mark a slot as most-recently-used."""
        self.entries.remove(slot)
        self.entries.append(slot)

    def store(self, slot: ObjectTypeContextSlot) -> None:
        """Publish a freshly built slot as the most-recently-used entry, evicting the oldest."""
        tokens = (slot.model_token, slot.namespace_token, slot.object_types_token)
        self.entries = [
            entry
            for entry in self.entries
            if (entry.model_token, entry.namespace_token, entry.object_types_token) != tokens
        ]
        self.entries.append(slot)
        del self.entries[: -self.max_entries]


@dataclass(slots=True)
class GatewayState:
    """Every runtime collaborator the gateway keeps for the lifetime of the app."""

    ua_startup_mode: str = "unknown"

    # OPC UA adapter and the model built from it.
    opcua_client: OpcUaClientProtocol | None = None
    model_builder: ModelBuilder | None = None
    model_cache: BuildResult | None = None
    model_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    model_build_task: asyncio.Task[BuildResult] | None = None
    model_preload_task: asyncio.Task[None] | None = None
    model_refresh_task: asyncio.Task[None] | None = None
    opcua_retry_task: asyncio.Task[None] | None = None

    # Object-type context machinery.
    object_type_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    object_type_context_cache: ObjectTypeContextCache | None = None
    object_type_warm_task: asyncio.Task[None] | None = None
    object_type_refresh_task: asyncio.Task[None] | None = None
    object_types_token_memo: tuple[list[Any], str] | None = None

    # Namespace snapshot shared by every per-request model query service.
    namespace_query_cache: NamespaceSnapshotCache = field(default_factory=NamespaceSnapshotCache)

    # Subscription runtime.
    subscription_service: SubscriptionServicePort | None = None

    # Diagnostics cache for the /ua routes.
    ua_status_snapshot: UaStatusSnapshot | None = None

    # MCP adapter state. Tool definitions and prompt metadata are owned by the MCP layer,
    # so their shapes stay opaque here.
    mcp_allowed_origins: list[str] = field(default_factory=list)
    mcp_tools: Mapping[str, Any] = field(default_factory=dict)
    mcp_overrides_applied: bool = False
    mcp_api_prefix: str = ""
    mcp_transport_sessions: dict[str, McpTransportSession] = field(default_factory=dict)
    mcp_prompts: PromptRegistry | None = None
    mcp_model_provider: Callable[[Request], Awaitable[BuildResult]] | None = None


def install_gateway_state(app: FastAPI, state: GatewayState) -> GatewayState:
    """Install the typed container on ``app.state`` (called by the composition root)."""
    app.state = cast(State, state)
    return state


def gateway_state(target: FastAPI | Request | Any) -> GatewayState:
    """Typed view of the ``GatewayState`` installed by :func:`install_gateway_state`.

    Accepts an app, a request, and request-like test doubles: requests resolve through
    ``request.app.state`` (Starlette's ``Request.state`` is a per-request bag backed by
    ``scope["state"]``, not the app container), app-like objects through ``.state``.
    """
    app = getattr(target, "app", None)
    if app is not None:
        return cast(GatewayState, app.state)
    return cast(GatewayState, target.state)
