"""Application port for subscription orchestration dependencies."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from i3x_server.schemas.state import BuildResult


class SubscriptionUpdatePort(Protocol):
    # Read-only properties: protocol *attributes* are invariant, so the concrete
    # dataclasses returned by the infrastructure service only satisfy the port when
    # members are declared covariantly (L8).
    @property
    def sequence_number(self) -> int: ...

    @property
    def element_id(self) -> str: ...

    @property
    def node_id(self) -> str: ...

    @property
    def value(self) -> object: ...

    @property
    def quality(self) -> str: ...

    @property
    def timestamp(self) -> str: ...


class SubscriptionSyncResultPort(Protocol):
    @property
    def updates(self) -> Sequence[SubscriptionUpdatePort]: ...

    @property
    def queue_overflow(self) -> bool: ...

    @property
    def dropped_from_sequence(self) -> int | None: ...

    @property
    def dropped_to_sequence(self) -> int | None: ...

    @property
    def stream_active(self) -> bool: ...


class SubscriptionDeleteResultPort(Protocol):
    @property
    def success(self) -> bool: ...

    @property
    def subscription_id(self) -> str: ...

    # A plain `Mapping`, not a `TypedDict`: the service builds the error payload as a
    # dict; the keys (`code`, `message`) are defined by the i3X error envelope.
    @property
    def error(self) -> Mapping[str, object] | None: ...


class SubscriptionDetailPort(Protocol):
    @property
    def subscription_id(self) -> str: ...

    @property
    def client_id(self) -> str | None: ...

    @property
    def display_name(self) -> str | None: ...

    @property
    def monitored_objects(self) -> Sequence[Mapping[str, object]]: ...

    @property
    def mode(self) -> str: ...


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
    ) -> Sequence[SubscriptionUpdatePort] | None: ...

    async def deactivate_stream(self, subscription_id: str, stream_generation: int) -> None: ...

    async def is_stream_active(self, subscription_id: str, stream_generation: int) -> bool: ...

    async def delete_subscriptions(
        self, client_id: str | None, subscription_ids: list[str]
    ) -> Sequence[SubscriptionDeleteResultPort]: ...

    async def list_subscriptions(
        self,
        client_id: str | None,
        subscription_ids: list[str] | None = None,
    ) -> Sequence[SubscriptionDetailPort]: ...

    async def close(self) -> None: ...
