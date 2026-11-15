"""A termination signal must reach Uvicorn's exit handler, not only the gateway's early cleanup.

Uvicorn installs ``handle_exit`` with ``signal.signal()`` around the whole serve loop, deliberately
so it can restore the previous handlers afterwards. The lifespan then registers its own handler so
SSE and MCP streams close *before* Uvicorn's "Waiting for connections to close" phase. On Linux that
registration goes through ``loop.add_signal_handler()``, which *replaces* Uvicorn's handler rather
than chaining it - only the Windows fallback chains. So ``should_exit`` was never set: SIGTERM left
the gateway serving, ``--timeout-graceful-shutdown`` never applied, and Docker's ``stop_grace_period``
SIGKilled the process mid-cleanup instead of draining it.

The loop below is a stand-in that mirrors asyncio's behaviour: ``add_signal_handler`` records the
callback and installs a real ``signal.signal()`` wrapper over it, which is what displaces Uvicorn's
handler. That keeps the assertions identical on Windows and Linux.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable
from typing import Any, cast

import pytest
from fastapi import FastAPI

from i3x_server.application.state import GatewayState, McpTransportSession, install_gateway_state
from i3x_server.bootstrap.runtime_setup import ShutdownSignalHandlers, install_shutdown_signal_handlers
from i3x_server.infrastructure.subscriptions.service import SubscriptionService


class _RecordingLoop:
    """Event-loop stand-in that records ``add_signal_handler`` and mirrors its signal effect."""

    def __init__(self) -> None:
        self.callbacks: dict[int, tuple[Callable[..., None], tuple[Any, ...]]] = {}

    def add_signal_handler(self, sig: int, callback: Callable[..., None], *args: Any) -> None:
        self.callbacks[sig] = (callback, args)
        signal.signal(sig, lambda signum, frame: callback(*args))

    def remove_signal_handler(self, sig: int) -> bool:
        self.callbacks.pop(sig, None)
        signal.signal(sig, signal.SIG_DFL)
        return True


class _RecordingSubscriptionService:
    """Minimal double for the runtime's early-shutdown hook."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def initiate_shutdown(self) -> None:
        self.events.append("streams_closed")


def _install(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    sig: signal.Signals,
    previous: Any = None,
) -> tuple[_RecordingLoop, ShutdownSignalHandlers, GatewayState, Callable[..., None]]:
    """Register the lifespan's handler over a stand-in for Uvicorn's exit handler."""

    loop = _RecordingLoop()
    monkeypatch.setattr(asyncio, "get_event_loop", lambda: loop)
    app = FastAPI()
    state = install_gateway_state(app, GatewayState())

    if previous is None:

        def uvicorn_handler(signum: int, frame: Any) -> None:
            events.append("exit_requested")

        previous = uvicorn_handler
    signal.signal(sig, previous)
    handlers = install_shutdown_signal_handlers(app, cast(SubscriptionService, _RecordingSubscriptionService(events)))
    return loop, handlers, state, cast(Callable[..., None], previous)


def _fire(loop: _RecordingLoop, sig: signal.Signals) -> None:
    """Deliver the signal the way asyncio does: run the registered callback on the loop thread."""

    callback, args = loop.callbacks[sig]
    callback(*args)


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_termination_signal_runs_the_handler_it_displaced(sig: signal.Signals, monkeypatch: pytest.MonkeyPatch) -> None:
    """The displaced Uvicorn handler must still run, otherwise ``should_exit`` stays False."""

    events: list[str] = []
    loop, handlers, _state, previous = _install(monkeypatch, events, sig)
    try:
        assert signal.getsignal(sig) is not previous, "registration must displace the previous handler"

        _fire(loop, sig)

        assert events == ["streams_closed", "exit_requested"], (
            "streams must close first, then the previous handler must request the exit"
        )
    finally:
        handlers.restore()
        signal.signal(sig, signal.SIG_DFL)


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_restore_reinstalls_the_displaced_handler(sig: signal.Signals, monkeypatch: pytest.MonkeyPatch) -> None:
    """Shutdown must give the handler back, not leave the signal at the default disposition."""

    events: list[str] = []
    loop, handlers, _state, previous = _install(monkeypatch, events, sig)
    displaced = signal.getsignal(sig)
    try:
        handlers.restore()
        assert signal.getsignal(sig) is not displaced, "restore must undo the registration"
        assert signal.getsignal(sig) is previous, "the handler that was displaced must come back"
    finally:
        signal.signal(sig, signal.SIG_DFL)


def test_termination_signal_closes_live_mcp_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    """The early close must still empty the transport sessions when it chains through."""

    events: list[str] = []
    loop, handlers, state, _previous = _install(monkeypatch, events, signal.SIGINT)
    stream: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    state.mcp_transport_sessions["session-1"] = McpTransportSession(protocol_version="2025-06-18", streams={stream})
    try:
        _fire(loop, signal.SIGINT)

        assert stream.qsize() == 1
        assert stream.get_nowait() is None
        assert state.mcp_transport_sessions == {}
        assert events == ["streams_closed", "exit_requested"]
    finally:
        handlers.restore()
        signal.signal(signal.SIGINT, signal.SIG_DFL)


@pytest.mark.parametrize("disposition", [signal.SIG_DFL, signal.SIG_IGN])
def test_termination_signal_tolerates_a_non_callable_previous_handler(
    disposition: signal.Handlers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A supervisor that leaves the signal ignored must not make the early close fail."""

    events: list[str] = []
    loop, handlers, _state, _previous = _install(monkeypatch, events, signal.SIGINT, previous=disposition)
    try:
        _fire(loop, signal.SIGINT)
        assert events == ["streams_closed"], "the close must run without chaining a non-callable handler"
    finally:
        handlers.restore()
        signal.signal(signal.SIGINT, signal.SIG_DFL)
