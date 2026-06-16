"""
Subscription orchestration service.

Manages subscription lifecycle, monitored items registration, and update
delivery (sync polling and SSE streaming).
"""

from __future__ import annotations

import logging

from fastapi import Request

from i3x_server.application.ports.subscription import SubscriptionServicePort
from i3x_server.application.services.subscription_mapper import (
    CreateSubscriptionDto,
    DeleteSubscriptionsDto,
    ListSubscriptionsDto,
    PendingUpdatesDto,
    RegisterMonitoredItemsDto,
    map_create_subscription,
    map_delete_subscriptions,
    map_list_subscriptions,
    map_pending_updates,
    map_register_monitored_items,
)
from i3x_server.domain.ports.opcua import OpcUaClientProtocol
from i3x_server.errors import i3x_http_error
from i3x_server.schemas.state import BuildResult

logger = logging.getLogger(__name__)


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
            raise i3x_http_error(
                502,
                "SubscriptionError",
                "Failed to create subscription",
                {"cause": str(exc)},
            ) from exc

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

            # Register items in subscription service
            registered = await self.subscription_service.register_items(
                client_id,
                subscription_id,
                element_ids,
                max_depth=max_depth or 1,
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
            raise i3x_http_error(
                502,
                "SubscriptionError",
                "Failed to register monitored items",
                {"cause": str(exc)},
            ) from exc

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
            raise i3x_http_error(
                502,
                "SubscriptionError",
                "Failed to retrieve subscription updates",
                {"cause": str(exc)},
            ) from exc

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
            raise i3x_http_error(
                502,
                "SubscriptionError",
                "Failed to delete subscriptions",
                {"cause": str(exc)},
            ) from exc

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
            raise i3x_http_error(
                502,
                "SubscriptionError",
                "Failed to list subscriptions",
                {"cause": str(exc)},
            ) from exc
