"""asyncua transport recovery: supervised client, reconnect hand-off, error classification.

asyncua's connection supervisor owns reconnects; these helpers reserve the recovery path
independently of the application request budget, hand a server-dropped session over to the
supervisor right away, and classify failures so callers retry only on connection loss and
never on request timeouts. Tests and monkeypatching target this module.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar, cast

from asyncua import ua
from asyncua.client.client import Client, _supervisor_owns_requests
from asyncua.client.ua_client import UaClient, UaClientState
from asyncua.common.shortcuts import Shortcuts
from asyncua.common.utils import Buffer
from asyncua.ua.status_codes import StatusCodes
from asyncua.ua.uaerrors import UaStatusCodeError

if TYPE_CHECKING:
    from i3x_server.infrastructure.opcua.client import OpcUaClient

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

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


async def _wait_for_reconnect(client: OpcUaClient, exc: Exception) -> None:
    """Wait until asyncua's supervisor has re-established the session after `exc`.

    Raises ConnectionError if the client is not connected again within the reconnect
    request timeout, so callers fail fast instead of piling up behind a dead connection.
    """
    uaclient = client._client.uaclient
    if (
        uaclient.state is UaClientState.CONNECTED
        and isinstance(exc, UaStatusCodeError)
        and exc.code in _CONNECTION_LOST_STATUS_CODES
    ):
        # The transport is up but the server dropped the session/channel; asyncua's
        # watchdog would only notice on its next tick, so hand the loss over right away.
        logger.warning(
            "OPC UA session lost endpoint=%s status=%s; triggering asyncua reconnect",
            client._endpoint,
            exc,
        )
        uaclient.notify_transport_lost()
    timeout = client._reconnect_request_timeout_seconds
    async with uaclient.subscribe_state() as states:
        try:
            await states.wait_for_state(UaClientState.CONNECTED, timeout=timeout)
        except asyncio.TimeoutError:
            raise ConnectionError(
                f"OPC UA client did not reconnect within {timeout:.1f}s (state={uaclient.state.value})"
            ) from exc


def _should_retry_after_disconnect(exc: Exception) -> bool:
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


def _is_transport_read_failure(exc: Exception) -> bool:
    return (
        isinstance(exc, (TimeoutError, ConnectionError))
        or _should_retry_after_disconnect(exc)
        or (
            isinstance(exc, UaStatusCodeError)
            and exc.code in {StatusCodes.BadTimeout, StatusCodes.BadRequestTimeout, StatusCodes.BadServerTooBusy}
        )
    )


def _is_too_many_operations_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "badtoomanyoperations" in text or "too many operations" in text


def _is_continuation_point_invalid(exc: Exception) -> bool:
    """BrowseNext points dropped by session recreation report this status (L10)."""
    if isinstance(exc, UaStatusCodeError):
        return bool(exc.code == StatusCodes.BadContinuationPointInvalid)
    return "badcontinuationpointinvalid" in str(exc).lower()


async def _retry_call(client: OpcUaClient, awaitable: Awaitable[_T]) -> _T:
    """Await a post-reconnect retry; a failing retry counts as a failed request (L10).

    Only the first attempt used to be recorded, so retries after a reconnect
    left `failed_request_count` undercounting.
    """
    try:
        return await awaitable
    except Exception:
        client._record_failed_request()
        raise
