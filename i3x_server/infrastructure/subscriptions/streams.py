"""SSE stream activation and the update wait cycle for one subscription.

``wait_for_updates`` races the per-subscription event against service-wide shutdown and a
wall-clock timeout; on timeout it may refresh values through the service so a native
subscription that the server never notifies still delivers something.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from math import ceil
from typing import TYPE_CHECKING

from i3x_server.config.settings import stream_debug_enabled
from i3x_server.infrastructure.subscriptions.state import SubscriptionUpdate

if TYPE_CHECKING:
    from i3x_server.infrastructure.subscriptions.service import SubscriptionService

logger = logging.getLogger(__name__)


async def _activate_stream(service: SubscriptionService, client_id: str | None, subscription_id: str) -> int | None:
    service._ensure_cleanup_task()
    async with service._lock:
        subscription_state = service._subscriptions.get(subscription_id)
        if subscription_state is None or (client_id is not None and subscription_state.client_id != client_id):
            return None
        subscription_state.active_stream_generation += 1
        subscription_state.stream_connected = True
        service._touch(subscription_state)
        subscription_state.update_event.set()
        if stream_debug_enabled():
            logger.info(
                "Subscription service activate stream subscription_id=%s generation=%s monitored_nodes=%s mode=%s",
                subscription_id,
                subscription_state.active_stream_generation,
                len(subscription_state.monitored_node_ids),
                subscription_state.mode,
            )
        return subscription_state.active_stream_generation


async def _deactivate_stream(service: SubscriptionService, subscription_id: str, stream_generation: int) -> None:
    async with service._lock:
        subscription_state = service._subscriptions.get(subscription_id)
        if subscription_state is None:
            return
        if subscription_state.active_stream_generation == stream_generation:
            subscription_state.stream_connected = False
            service._touch(subscription_state)
            if stream_debug_enabled():
                logger.info(
                    "Subscription service deactivate stream subscription_id=%s generation=%s",
                    subscription_id,
                    stream_generation,
                )


async def _is_stream_active(service: SubscriptionService, subscription_id: str, stream_generation: int) -> bool:
    async with service._lock:
        subscription_state = service._subscriptions.get(subscription_id)
        if subscription_state is None:
            return False
        return subscription_state.active_stream_generation == stream_generation


async def _wait_for_updates(
    service: SubscriptionService,
    client_id: str | None,
    subscription_id: str,
    after_sequence: int,
    timeout_seconds: float = 15.0,
) -> list[SubscriptionUpdate] | None:
    service._ensure_cleanup_task()
    should_refresh_after_timeout = False
    refresh_interval_seconds: float | None = None
    refresh_timeout_threshold = 1
    async with service._lock:
        subscription_state = service._subscriptions.get(subscription_id)
        if subscription_state is None or (client_id is not None and subscription_state.client_id != client_id):
            return None

        service._touch(subscription_state)

        async with subscription_state.lock:
            current = service._updates_after(subscription_state, after_sequence)
            if current:
                subscription_state.native_timeout_count = 0
                return current

            subscription_state.update_event.clear()
            wait_event = subscription_state.update_event

        should_refresh_after_timeout = (
            subscription_state.mode == "native"
            and bool(subscription_state.monitored_node_ids)
            and service._native_timeout_refresh_mode != "strict"
        )
        refresh_interval_seconds = subscription_state.native_refresh_interval_seconds
        if should_refresh_after_timeout and service._native_timeout_refresh_mode == "adaptive":
            interval_seconds = refresh_interval_seconds or service._interval_seconds
            refresh_timeout_threshold = max(1, ceil(interval_seconds / max(0.001, timeout_seconds)))

    # Race: per-subscription update, service-wide shutdown, or wall-clock timeout.
    # Using asyncio.wait so we can also abort early on server shutdown without
    # replacing Uvicorn's own SIGINT/SIGTERM handling.
    _wakeup = asyncio.ensure_future(wait_event.wait())
    _abort = asyncio.ensure_future(service._shutdown_event.wait())
    try:
        done, _pending = await asyncio.wait(
            {_wakeup, _abort},
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        # ``asyncio.wait`` does not cancel the futures it leaves pending - and it also does not
        # cancel them when *this* coroutine is cancelled (client disconnect, shutdown). Both
        # futures must be cancelled and awaited here, otherwise every SSE stream leaks two
        # orphaned tasks ("Task was destroyed but it is pending").
        _wakeup.cancel()
        _abort.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.gather(_wakeup, _abort, return_exceptions=True)

    if service._shutdown_event.is_set():
        # Server is shutting down; tell the SSE generator to close gracefully.
        return None

    if not done:  # wall-clock timeout - same logic as the former except TimeoutError block
        should_execute_refresh = False
        timeout_count = 0
        async with service._lock:
            subscription_state = service._subscriptions.get(subscription_id)
            if subscription_state is None or (client_id is not None and subscription_state.client_id != client_id):
                return None
            if should_refresh_after_timeout:
                async with subscription_state.lock:
                    subscription_state.native_timeout_count += 1
                    timeout_count = subscription_state.native_timeout_count
                if service._native_timeout_refresh_mode == "adaptive":
                    should_execute_refresh = timeout_count >= refresh_timeout_threshold
                else:
                    should_execute_refresh = True

        if should_refresh_after_timeout:
            if stream_debug_enabled() and service._native_timeout_refresh_mode == "adaptive":
                logger.info(
                    "Subscription service wait timeout native subscription_id=%s after_sequence=%s "
                    "timeout_count=%d threshold=%d refresh_interval_s=%.3f",
                    subscription_id,
                    after_sequence,
                    timeout_count,
                    refresh_timeout_threshold,
                    refresh_interval_seconds or service._interval_seconds,
                )
            if should_execute_refresh:
                async with service._lock:
                    subscription_state = service._subscriptions.get(subscription_id)
                    if subscription_state is None or (
                        client_id is not None and subscription_state.client_id != client_id
                    ):
                        return None
                    async with subscription_state.lock:
                        subscription_state.native_timeout_count = 0
            if stream_debug_enabled() and should_execute_refresh:
                logger.info(
                    "Subscription service wait timeout refresh subscription_id=%s after_sequence=%s",
                    subscription_id,
                    after_sequence,
                )
            if should_execute_refresh:
                await service._refresh_changed_values(subscription_id)
        async with service._lock:
            subscription_state = service._subscriptions.get(subscription_id)
            if subscription_state is None or (client_id is not None and subscription_state.client_id != client_id):
                return None
            service._touch(subscription_state)
            async with subscription_state.lock:
                updates = service._updates_after(subscription_state, after_sequence)
            if stream_debug_enabled():
                logger.info(
                    "Subscription service wait timeout result subscription_id=%s updates=%s after_sequence=%s",
                    subscription_id,
                    len(updates),
                    after_sequence,
                )
            return updates

    async with service._lock:
        subscription_state = service._subscriptions.get(subscription_id)
        if subscription_state is None or (client_id is not None and subscription_state.client_id != client_id):
            return None
        service._touch(subscription_state)
        async with subscription_state.lock:
            subscription_state.native_timeout_count = 0
            return service._updates_after(subscription_state, after_sequence)
