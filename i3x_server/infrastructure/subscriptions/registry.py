"""Subscription registry lifecycle: creation, listing, TTL expiry, shutdown.

The functions take the owning ``SubscriptionService`` and operate on its state under its
lock, the same convention as ``monitoring.py``. ``service.py`` keeps thin delegating
methods so the class stays the single entry point for routes, tests, and the monitoring
runtime; cross-cutting helpers are always called through the service, so patching a
service method still affects every path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from uuid import uuid4

from i3x_server.domain.errors import AdmissionLimitError
from i3x_server.infrastructure.subscriptions import monitoring, state
from i3x_server.infrastructure.subscriptions.state import SubscriptionDeleteResult, SubscriptionDetail

if TYPE_CHECKING:
    from i3x_server.infrastructure.subscriptions.service import SubscriptionService

logger = logging.getLogger(__name__)


def _remember_event_loop(service: SubscriptionService) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if service._event_loop is None or service._event_loop.is_closed():
        service._event_loop = loop


def _now_monotonic(service: SubscriptionService) -> float:
    _remember_event_loop(service)
    return asyncio.get_running_loop().time()


def _touch(service: SubscriptionService, state_obj: state._SubscriptionState) -> None:
    state_obj.last_activity_monotonic = _now_monotonic(service)


def _ensure_cleanup_task(service: SubscriptionService) -> None:
    if service._closed:
        # ``close()`` already tore the runtime down; a late call (TTL sweep, datachange callback,
        # reconnect, sync after shutdown) must not resurrect background tasks.
        return
    if service._cleanup_task is None or service._cleanup_task.done():
        service._cleanup_task = asyncio.create_task(service._cleanup_loop())
    if service._node_datachange_task is None or service._node_datachange_task.done():
        service._node_datachange_task = asyncio.create_task(service._node_datachange_loop())
    if service._shared_polling_task is None or service._shared_polling_task.done():
        service._shared_polling_task = asyncio.create_task(monitoring._shared_polling_loop(service))


async def _cleanup_loop(service: SubscriptionService) -> None:
    try:
        while True:
            await asyncio.sleep(max(0.1, service._interval_seconds))
            await service._expire_subscriptions()
    except asyncio.CancelledError:
        return


async def _expire_subscriptions(service: SubscriptionService) -> None:
    # Keep subscription references out of the long-lived cleanup loop's frame.
    stale_ids: list[str] = []
    now = _now_monotonic(service)
    async with service._lock:
        for subscription_state in service._subscriptions.values():
            if now - subscription_state.last_activity_monotonic >= service._ttl_seconds:
                stale_ids.append(subscription_state.subscription_id)

        removed: list[state._SubscriptionState] = []
        for subscription_id in stale_ids:
            if subscription_id not in service._subscriptions:
                continue
            subscription_state = service._subscriptions.pop(subscription_id)
            subscription_state.update_event.set()
            removed.append(subscription_state)

    for subscription_state in removed:
        logger.warning("Subscription expired by TTL subscription_id=%s", subscription_state.subscription_id)
        await service._release_all_nodes(subscription_state)


async def _handle_client_reconnect(service: SubscriptionService) -> None:
    # Native-bin resync lives in ``monitoring._handle_client_reconnect``; this stays the
    # listener entry point registered on the OPC UA client.
    await monitoring._handle_client_reconnect(service)


def _initiate_shutdown(service: SubscriptionService) -> None:
    """Signal all active SSE streams to close immediately.

    Call this as early as possible (e.g. from a SIGINT/SIGTERM handler)
    so that streaming HTTP connections are released *before* Uvicorn
    enters its "Waiting for connections to close" phase.  The full
    ``close()`` coroutine still needs to be awaited afterwards to stop
    OPC UA subscriptions and disconnect the client.
    """
    service._shutdown_event.set()
    # Also pulse every per-subscription event so that tasks blocked in
    # wait_for_updates() wake up immediately without waiting for their
    # next timeout cycle.
    for subscription_state in service._subscriptions.values():
        subscription_state.update_event.set()


async def _close(service: SubscriptionService) -> None:
    # Signal all waiting SSE stream tasks to wake up and terminate *before*
    # any cleanup so that HTTP connections can be closed while Uvicorn is
    # still in its "Waiting for connections to close" phase.
    service._shutdown_event.set()
    service._closed = True
    async with service._lock:
        subscriptions = list(service._subscriptions.values())

    for subscription in subscriptions:
        # Signal any waiting stream tasks (wait_for_updates) to wake up and terminate
        subscription.update_event.set()
        await service._release_all_nodes(subscription)

    async with service._lock:
        service._subscriptions.clear()

    cleanup_task = service._cleanup_task
    service._cleanup_task = None
    if cleanup_task is not None:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass

    node_datachange_task = service._node_datachange_task
    service._node_datachange_task = None
    if node_datachange_task is not None:
        node_datachange_task.cancel()
        try:
            await node_datachange_task
        except asyncio.CancelledError:
            pass

    shared_polling_task = service._shared_polling_task
    service._shared_polling_task = None
    if shared_polling_task is not None:
        shared_polling_task.cancel()
        try:
            await shared_polling_task
        except asyncio.CancelledError:
            pass

    remove_listener = getattr(service._opcua_client, "remove_reconnect_listener", None)
    if callable(remove_listener):
        try:
            remove_listener(service._reconnect_listener)
        except Exception:
            logger.debug("Failed to remove reconnect listener", exc_info=True)


async def _create_subscription(
    service: SubscriptionService,
    client_id: str | None,
    display_name: str | None = None,
) -> SubscriptionDetail:
    service._ensure_cleanup_task()
    subscription_id = f"sub-{uuid4()}"
    subscription_state = state._SubscriptionState(
        subscription_id=subscription_id,
        client_id=client_id,
        display_name=display_name,
        monitored_objects={},
        last_activity_monotonic=_now_monotonic(service),
    )
    async with service._lock:
        active_count = len(service._subscriptions)
        if active_count >= service._max_subscriptions:
            # Rejected before insertion: a partially created subscription would leak state
            # and count towards the cap it just exceeded.
            raise AdmissionLimitError(
                429,
                "SubscriptionLimit",
                "Subscription limit reached",
                {"maxSubscriptions": service._max_subscriptions, "activeSubscriptions": active_count},
            )
        service._subscriptions[subscription_id] = subscription_state
        # Snapshot inside the lock: ``register_items`` mutates ``state.monitored_objects`` and a
        # detail built after release can race with it ("dictionary changed size during iteration").
        detail = service._to_detail(subscription_state)
    logger.info(
        "Subscription created subscription_id=%s client_id=%s display_name=%s",
        subscription_id,
        client_id,
        display_name,
    )
    return detail


async def _list_subscriptions(
    service: SubscriptionService,
    client_id: str | None,
    subscription_ids: list[str] | None = None,
) -> list[SubscriptionDetail]:
    async with service._lock:
        if client_id is None:
            values = list(service._subscriptions.values())
        else:
            values = [item for item in service._subscriptions.values() if item.client_id == client_id]
        if subscription_ids is not None:
            allowed = set(subscription_ids)
            values = [item for item in values if item.subscription_id in allowed]
        # Snapshot inside the lock for the same reason as ``create_subscription``.
        return [service._to_detail(item) for item in values]


async def _delete_subscriptions(
    service: SubscriptionService,
    client_id: str | None,
    subscription_ids: list[str],
) -> list[SubscriptionDeleteResult]:
    removed: list[state._SubscriptionState] = []
    results: list[SubscriptionDeleteResult] = []

    async with service._lock:
        for subscription_id in subscription_ids:
            subscription_state = service._subscriptions.get(subscription_id)
            if subscription_state is None:
                results.append(
                    SubscriptionDeleteResult(
                        success=False,
                        subscription_id=subscription_id,
                        error={"code": 404, "message": "Subscription not found"},
                    )
                )
                continue
            if client_id is not None and subscription_state.client_id != client_id:
                results.append(
                    SubscriptionDeleteResult(
                        success=False,
                        subscription_id=subscription_id,
                        error={"code": 404, "message": "Subscription not found"},
                    )
                )
                continue
            service._subscriptions.pop(subscription_id, None)
            removed.append(subscription_state)
            subscription_state.update_event.set()
            results.append(SubscriptionDeleteResult(success=True, subscription_id=subscription_id))

    for subscription_state in removed:
        await service._release_all_nodes(subscription_state)
        logger.info(
            "Subscription deleted subscription_id=%s client_id=%s monitored_nodes=%d",
            subscription_state.subscription_id,
            subscription_state.client_id,
            len(subscription_state.monitored_node_ids),
        )

    return results


def _to_detail(service: SubscriptionService, subscription_state: state._SubscriptionState) -> SubscriptionDetail:
    monitored = [
        {"elementId": element_id, "maxDepth": max_depth}
        for element_id, max_depth in subscription_state.monitored_objects.items()
    ]
    return SubscriptionDetail(
        subscription_id=subscription_state.subscription_id,
        client_id=subscription_state.client_id,
        display_name=subscription_state.display_name,
        monitored_objects=monitored,
        mode=subscription_state.mode,
    )
