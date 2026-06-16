"""Application port for subscription orchestration dependencies."""

from __future__ import annotations

from typing import Protocol, TypedDict

from i3x_server.schemas.state import BuildResult


class SubscriptionUpdatePort(Protocol):
    sequence_number: int
    element_id: str
    node_id: str
    value: object
    quality: str
    timestamp: str


class SubscriptionSyncResultPort(Protocol):
    updates: list[SubscriptionUpdatePort]
    queue_overflow: bool
    dropped_from_sequence: int | None
    dropped_to_sequence: int | None
    stream_active: bool


class SubscriptionDeleteResultPort(Protocol):
    class ErrorPayload(TypedDict):
        code: int
        message: str

    success: bool
    subscription_id: str
    error: ErrorPayload | None


class SubscriptionDetailPort(Protocol):
    subscription_id: str
    client_id: str | None
    display_name: str | None
    monitored_objects: list[dict[str, object]]
    mode: str


class SubscriptionServicePort(Protocol):
    async def create_subscription(
        self, client_id: str | None, display_name: str | None = None
    ) -> SubscriptionDetailPort: ...

    async def register_items(
        self,
        client_id: str | None,
        subscription_id: str,
        element_ids: list[str],
        max_depth: int,
        model: BuildResult,
    ) -> bool: ...

    async def unregister_items(
        self,
        client_id: str | None,
        subscription_id: str,
        element_ids: list[str],
        model: BuildResult,
    ) -> bool: ...

    async def activate_stream(self, client_id: str | None, subscription_id: str) -> int | None: ...

    async def sync(
        self,
        client_id: str | None,
        subscription_id: str,
        acknowledge_sequence: int | None = None,
        allow_when_stream_active: bool = False,
    ) -> SubscriptionSyncResultPort | None: ...

    async def wait_for_updates(
        self,
        client_id: str | None,
        subscription_id: str,
        after_sequence: int,
        timeout_seconds: float,
    ) -> list[SubscriptionUpdatePort] | None: ...

    async def deactivate_stream(self, subscription_id: str, stream_generation: int) -> None: ...

    async def is_stream_active(self, subscription_id: str, stream_generation: int) -> bool: ...

    async def has_active_stream(self, client_id: str | None, subscription_id: str) -> bool | None: ...

    async def delete_subscriptions(
        self, client_id: str | None, subscription_ids: list[str]
    ) -> list[SubscriptionDeleteResultPort]: ...

    async def list_subscriptions(
        self,
        client_id: str | None,
        subscription_ids: list[str] | None = None,
    ) -> list[SubscriptionDetailPort]: ...

    async def close(self) -> None: ...
