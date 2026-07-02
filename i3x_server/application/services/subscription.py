"""
Subscription orchestration service.

Manages subscription lifecycle, monitored items registration, and update
delivery (sync polling and SSE streaming).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Literal, NoReturn, TypedDict

from fastapi import HTTPException, Request

from i3x_server.application.ports.subscription import (
    SubscriptionServicePort,
    SubscriptionSyncResultPort,
    SubscriptionUpdatePort,
)
from i3x_server.application.services.subscription_mapper import (
    CreateSubscriptionDto,
    DeleteSubscriptionsDto,
    ListSubscriptionsDto,
    PendingUpdatesDto,
    RegisterMonitoredItemsDto,
    SubscriptionBulkItemDto,
    map_create_subscription,
    map_delete_subscription_items,
    map_delete_subscriptions,
    map_list_subscriptions,
    map_pending_updates,
    map_register_monitored_items,
    map_subscription_detail_bulk_items,
)
from i3x_server.domain.ports.opcua import OpcUaClientProtocol
from i3x_server.errors import i3x_http_error
from i3x_server.schemas.state import BuildResult

logger = logging.getLogger(__name__)


class StreamSessionDto(TypedDict):
    clientId: str
    subscriptionId: str
    streamGeneration: int
    scopeRelaxed: bool
    acknowledged: SubscriptionSyncResultPort


class StreamEventDto(TypedDict):
    kind: Literal["connected", "keepalive", "close", "updates"]
    updates: list[SubscriptionUpdatePort]


def _raise_or_wrap_subscription_error(message: str, exc: Exception) -> NoReturn:
    if isinstance(exc, HTTPException):
        raise exc
    raise i3x_http_error(
        502,
        "SubscriptionError",
        message,
        {"cause": str(exc)},
    ) from exc


class SubscriptionAppService:
    """Orchestrates subscription lifecycle and update delivery."""

    def __init__(
        self,
        opcua_client: OpcUaClientProtocol,
        model: BuildResult,
        subscription_service: SubscriptionServicePort,
        request: Request | None = None,
    ):
        """Initialize service with dependencies.

        Args:
            opcua_client: OPC UA protocol client
            model: Pre-built model structure
            subscription_service: Low-level subscription service
            request: Optional FastAPI request
        """
        self.opcua_client = opcua_client
        self.model = model
        self.subscription_service = subscription_service
        self.request = request

    async def create_subscription(
        self,
        client_id: str,
        display_name: str | None = None,
    ) -> CreateSubscriptionDto:
        """Create a new subscription session.

        Args:
            client_id: Unique client identifier
            display_name: Optional human-readable name

        Returns:
            Subscription details with ID
        """
        try:
            subscription = await self.subscription_service.create_subscription(client_id, display_name)
            return map_create_subscription(subscription, client_id, display_name)
        except Exception as exc:
            _raise_or_wrap_subscription_error("Failed to create subscription", exc)

    async def register_monitored_items(
        self,
        subscription_id: str,
        client_id: str | None,
        element_ids: list[str],
        max_depth: int | None = None,
    ) -> RegisterMonitoredItemsDto:
        """Register objects for monitoring.

        Args:
            subscription_id: Subscription to register with
            client_id: Optional client identifier
            element_ids: Objects to monitor
            max_depth: Maximum composition depth

        Returns:
            Registration confirmation
        """
        try:
            # Validate client ID if required
            if not client_id or not client_id.strip():
                if self.request:
                    raise i3x_http_error(
                        400,
                        "InvalidArgument",
                        "'/subscriptions/items' requires a non-empty clientId",
                        {"field": "clientId"},
                    )

            # Omitted/null depth means monitor all descendants (unlimited traversal).
            effective_max_depth = 0 if max_depth is None else max(0, max_depth)

            # Register items in subscription service
            registered = await self.subscription_service.register_items(
                client_id,
                subscription_id,
                element_ids,
                max_depth=effective_max_depth,
                model=self.model,
            )
            if not registered:
                raise i3x_http_error(
                    404,
                    "NotFound",
                    "Subscription not found",
                    {"subscriptionId": subscription_id},
                )
            return map_register_monitored_items(subscription_id, element_ids)
        except Exception as exc:
            _raise_or_wrap_subscription_error("Failed to register monitored items", exc)

    async def unregister_monitored_items(
        self,
        subscription_id: str,
        client_id: str | None,
        element_ids: list[str],
    ) -> RegisterMonitoredItemsDto:
        """Unregister monitored objects from a subscription."""
        try:
            if not client_id or not client_id.strip():
                if self.request:
                    raise i3x_http_error(
                        400,
                        "InvalidArgument",
                        "'/subscriptions/unregister' requires a non-empty clientId",
                        {"field": "clientId"},
                    )

            unregistered = await self.subscription_service.unregister_items(
                client_id,
                subscription_id,
                element_ids,
                model=self.model,
            )
            if not unregistered:
                raise i3x_http_error(
                    404,
                    "NotFound",
                    "Subscription not found",
                    {"subscriptionId": subscription_id},
                )
            return map_register_monitored_items(subscription_id, element_ids)
        except Exception as exc:
            _raise_or_wrap_subscription_error("Failed to unregister monitored items", exc)

    async def begin_stream_session(
        self,
        subscription_id: str,
        client_id: str | None,
        acknowledge_sequence: int | None = None,
    ) -> StreamSessionDto:
        """Start a stream session and preload acknowledged updates."""
        try:
            if not client_id or not client_id.strip():
                raise i3x_http_error(
                    400,
                    "InvalidArgument",
                    "'/subscriptions/stream' requires a non-empty clientId",
                    {"field": "clientId"},
                )

            normalized_client_id = client_id.strip()
            stream_generation = await self.subscription_service.activate_stream(
                client_id=normalized_client_id,
                subscription_id=subscription_id,
            )
            scope_relaxed = False
            if stream_generation is None:
                stream_generation = await self.subscription_service.activate_stream(
                    client_id=None,
                    subscription_id=subscription_id,
                )
                if stream_generation is None:
                    raise i3x_http_error(
                        404,
                        "NotFound",
                        "Subscription not found",
                        {"subscriptionId": subscription_id},
                    )
                scope_relaxed = True

            acknowledged = await self.subscription_service.sync(
                client_id=None if scope_relaxed else normalized_client_id,
                subscription_id=subscription_id,
                acknowledge_sequence=acknowledge_sequence,
                allow_when_stream_active=True,
            )
            if acknowledged is None:
                raise i3x_http_error(
                    404,
                    "NotFound",
                    "Subscription not found",
                    {"subscriptionId": subscription_id},
                )

            return {
                "clientId": normalized_client_id,
                "subscriptionId": subscription_id,
                "streamGeneration": stream_generation,
                "scopeRelaxed": scope_relaxed,
                "acknowledged": acknowledged,
            }
        except Exception as exc:
            _raise_or_wrap_subscription_error("Failed to open subscription stream", exc)

    async def get_pending_updates(
        self,
        subscription_id: str,
        client_id: str | None,
        acknowledge_sequence: int | None = None,
    ) -> PendingUpdatesDto:
        """Retrieve pending updates (polling interface).

        Args:
            subscription_id: Subscription to query
            client_id: Optional client identifier
            acknowledge_sequence: Acknowledge up-to sequence

        Returns:
            Batch of pending updates
        """
        try:
            if not client_id or not client_id.strip():
                raise i3x_http_error(
                    400,
                    "InvalidArgument",
                    "'/subscriptions/sync' requires a non-empty clientId",
                    {"field": "clientId"},
                )

            # Get updates from subscription service
            sync_result = await self.subscription_service.sync(
                client_id,
                subscription_id,
                acknowledge_sequence=acknowledge_sequence,
            )
            if sync_result is None:
                raise i3x_http_error(
                    404,
                    "NotFound",
                    "Subscription not found",
                    {"subscriptionId": subscription_id},
                )
            if sync_result.stream_active:
                raise i3x_http_error(
                    400,
                    "InvalidState",
                    "Subscription stream is active",
                    {"subscriptionId": subscription_id},
                )
            return map_pending_updates(sync_result)
        except Exception as exc:
            _raise_or_wrap_subscription_error("Failed to retrieve subscription updates", exc)

    async def iter_stream_events(
        self,
        subscription_id: str,
        client_id: str,
        stream_generation: int,
        acknowledged_updates: list[SubscriptionUpdatePort],
        acknowledge_sequence: int | None = None,
        timeout_seconds: float = 2,
    ) -> AsyncIterator[StreamEventDto]:
        last_sequence = acknowledge_sequence or 0
        try:
            yield {"kind": "connected", "updates": []}
            if acknowledged_updates:
                last_update = acknowledged_updates[-1]
                if hasattr(last_update, "sequence_number"):
                    last_sequence = int(last_update.sequence_number)
                yield {"kind": "updates", "updates": acknowledged_updates}

            while True:
                is_active = await self.subscription_service.is_stream_active(subscription_id, stream_generation)
                if not is_active:
                    yield {"kind": "close", "updates": []}
                    return

                updates = await self.subscription_service.wait_for_updates(
                    client_id=client_id,
                    subscription_id=subscription_id,
                    after_sequence=last_sequence,
                    timeout_seconds=timeout_seconds,
                )
                if updates is None:
                    yield {"kind": "close", "updates": []}
                    return
                if not updates:
                    yield {"kind": "keepalive", "updates": []}
                    continue

                last_sequence = int(updates[-1].sequence_number)
                yield {"kind": "updates", "updates": updates}
        finally:
            await self.subscription_service.deactivate_stream(subscription_id, stream_generation)

    async def delete_subscriptions(
        self,
        subscription_ids: list[str],
        client_id: str | None = None,
    ) -> DeleteSubscriptionsDto:
        """Delete subscription sessions.

        Args:
            subscription_ids: Subscriptions to delete
            client_id: Optional client identifier

        Returns:
            Deletion confirmation
        """
        try:
            delete_results = await self.subscription_service.delete_subscriptions(client_id, subscription_ids)
            return map_delete_subscriptions(delete_results, requested=len(subscription_ids))
        except Exception as exc:
            _raise_or_wrap_subscription_error("Failed to delete subscriptions", exc)

    async def delete_subscription_items(
        self,
        subscription_ids: list[str],
        client_id: str | None = None,
    ) -> list[SubscriptionBulkItemDto]:
        """Delete subscriptions and return per-subscription bulk results."""
        try:
            delete_results = await self.subscription_service.delete_subscriptions(client_id, subscription_ids)
            return map_delete_subscription_items(delete_results)
        except Exception as exc:
            _raise_or_wrap_subscription_error("Failed to delete subscriptions", exc)

    async def list_subscriptions(
        self,
        client_id: str | None = None,
        subscription_ids: list[str] | None = None,
    ) -> ListSubscriptionsDto:
        """List active subscriptions.

        Args:
            client_id: Optional client filter
            subscription_ids: Optional subscription IDs filter

        Returns:
            List of subscription details
        """
        try:
            subscriptions = await self.subscription_service.list_subscriptions(
                client_id=client_id,
                subscription_ids=subscription_ids,
            )
            return map_list_subscriptions(subscriptions)
        except Exception as exc:
            _raise_or_wrap_subscription_error("Failed to list subscriptions", exc)

    async def list_subscription_items(
        self,
        client_id: str | None = None,
        subscription_ids: list[str] | None = None,
    ) -> list[SubscriptionBulkItemDto]:
        """List subscriptions and return per-subscription bulk results."""
        try:
            subscriptions = await self.subscription_service.list_subscriptions(
                client_id=client_id,
                subscription_ids=subscription_ids,
            )
            return map_subscription_detail_bulk_items(subscriptions, requested_ids=subscription_ids)
        except Exception as exc:
            _raise_or_wrap_subscription_error("Failed to list subscriptions", exc)
