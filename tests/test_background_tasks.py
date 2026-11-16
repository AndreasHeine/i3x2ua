"""A background task that dies must say so.

``asyncio.create_task`` returns a handle, but nothing here retrieved the exception of a loop that
ended early: the message appears only when the task is garbage collected (``Task exception was
never retrieved``), and the work the loop owned - subscription expiry, connection-state mirroring,
an OPC UA metadata refresh - stops without anything in the logs. Long-lived tasks are started
through one helper that keeps the handle and reports an unexpected failure once, while a
cancellation stays a cancellation because shutdown cancels these loops on purpose.

Acquiring an event loop with ``asyncio.get_event_loop()`` is deprecated since 3.12 and removed in
3.14, so the shipped package must reach the running loop instead.
"""

from __future__ import annotations

import asyncio
import gc
import logging
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import pytest

from i3x_server.background_tasks import start_tracked_task
from i3x_server.infrastructure.opcua import connection
from i3x_server.infrastructure.opcua.client import OpcUaClient, OpcUaClientProtocol
from i3x_server.infrastructure.subscriptions.service import SubscriptionService
from tests.opcua_fake import SubscriptionClientFake as FakeOpcUaClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "i3x_server"
TEST_LOGGER = logging.getLogger("tests.background_tasks")


@pytest.mark.asyncio
async def test_a_failed_background_task_is_reported_once(caplog: pytest.LogCaptureFixture) -> None:
    """The failure has to be a log line, not a message the interpreter prints at collection time."""

    async def dying_loop() -> None:
        raise RuntimeError("ttl sweep failed")

    task = start_tracked_task(dying_loop(), "subscription cleanup", TEST_LOGGER)
    with caplog.at_level(logging.ERROR, logger="tests.background_tasks"):
        with suppress(RuntimeError):
            await task

    assert any(
        "subscription cleanup" in record.getMessage() and "ttl sweep failed" in record.getMessage()
        for record in caplog.records
    ), "a loop that ended with an exception must be reported with its name and the error"


@pytest.mark.asyncio
async def test_a_cancelled_background_task_is_not_reported_as_a_failure(caplog: pytest.LogCaptureFixture) -> None:
    """Shutdown cancels these loops deliberately; that is not an error worth an ERROR line."""

    async def idle_loop() -> None:
        await asyncio.Event().wait()

    task = start_tracked_task(idle_loop(), "idle loop", TEST_LOGGER)
    await asyncio.sleep(0)
    task.cancel()
    with caplog.at_level(logging.ERROR, logger="tests.background_tasks"):
        with suppress(asyncio.CancelledError):
            await task

    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_the_failure_is_retrieved_even_when_no_caller_awaits_the_task(caplog: pytest.LogCaptureFixture) -> None:
    """Fire-and-forget means nobody awaits, so the helper must retrieve the exception itself.

    Without retrieval the only signal is asyncio's own ``Task exception was never retrieved`` from
    the loop's exception handler, which arrives whenever the interpreter collects the task.
    """

    async def dying_loop() -> None:
        raise RuntimeError("no caller awaits this")

    task = start_tracked_task(dying_loop(), "orphan loop", TEST_LOGGER)
    del task
    gc.collect()
    await asyncio.sleep(0)

    assert not any("never retrieved" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_a_failing_subscription_cleanup_loop_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The cleanup loop only guards cancellation, so a sweep error would end it silently."""

    service = SubscriptionService(cast(OpcUaClientProtocol, FakeOpcUaClient()), interval_seconds=0.05)

    async def _expire_that_fails() -> None:
        raise RuntimeError("ttl sweep failed")

    monkeypatch.setattr(service, "_expire_subscriptions", _expire_that_fails)
    service._ensure_cleanup_task()

    task = service._cleanup_task
    assert task is not None, "the service must keep the handle of the loop it started"
    with caplog.at_level(logging.ERROR, logger="i3x_server.infrastructure.subscriptions.registry"):
        with suppress(RuntimeError):
            await asyncio.wait_for(task, 1.0)

    assert any("ttl sweep failed" in record.getMessage() for record in caplog.records)

    # The loop is already dead and reported. Joining it during shutdown must not raise a second
    # time, or the teardown stops before releasing subscriptions.
    await service.close()
    assert service._cleanup_task is None


@pytest.mark.asyncio
async def test_a_dead_cleanup_loop_does_not_abort_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A background failure is reported once; shutdown only joins the task it owns."""

    client = FakeOpcUaClient()
    service = SubscriptionService(cast(OpcUaClientProtocol, client), interval_seconds=0.05)
    created = await service.create_subscription(client_id="c1", display_name=None)

    async def _expire_that_fails() -> None:
        raise RuntimeError("ttl sweep failed")

    monkeypatch.setattr(service, "_expire_subscriptions", _expire_that_fails)
    service._ensure_cleanup_task()
    task = service._cleanup_task
    assert task is not None
    with suppress(RuntimeError):
        await asyncio.wait_for(task, 1.0)

    await service.close()
    assert await service.sync("c1", created.subscription_id, acknowledge_sequence=None) is None


@pytest.mark.asyncio
async def test_a_failed_connection_state_watcher_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A dead watcher stops mirroring the transport state; the gateway would keep reporting Connected."""

    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")

    async def _watch_that_fails() -> None:
        raise RuntimeError("state subscription died")

    monkeypatch.setattr(client, "watch_connection_state", _watch_that_fails)
    connection.start_connection_tasks(client)

    watcher = client.connection_state_task
    assert watcher is not None, "the client must keep the handle of the watcher it started"
    with caplog.at_level(logging.ERROR, logger="i3x_server.infrastructure.opcua.connection"):
        with suppress(RuntimeError):
            await asyncio.wait_for(watcher, 1.0)

    assert any("state subscription died" in record.getMessage() for record in caplog.records)

    # Joining a watcher that already failed must not abort the disconnect that follows it.
    await connection.stop_connection_tasks(client)
    assert client.connection_state_task is None
    assert client.reconnect_handler_task is None


@pytest.mark.asyncio
async def test_a_failed_background_metadata_refresh_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stale cache keeps serving while the refresh runs unawaited, so a failed refresh is invisible."""

    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    cast(Any, client).namespace_infos_cache = (-1e9, ["stale"])

    async def _failing_load() -> list[Any]:
        raise RuntimeError("namespace browse failed")

    monkeypatch.setattr(client, "_load_namespace_infos", _failing_load)
    caplog.set_level(logging.ERROR, logger="i3x_server.infrastructure.opcua.metadata_cache")

    assert cast(list[Any], await client.get_namespace_infos()) == ["stale"]
    with suppress(RuntimeError):
        await cast(Any, client).metadata_load_tasks["namespace_infos"]
    await asyncio.sleep(0)

    assert any("namespace browse failed" in record.getMessage() for record in caplog.records)


def test_the_shipped_package_never_acquires_an_event_loop_the_deprecated_way() -> None:
    """``asyncio.get_event_loop()`` warns since 3.12 and raises in 3.14 without a running loop."""
    offenders = [
        str(path.relative_to(PROJECT_ROOT))
        for path in PACKAGE_ROOT.rglob("*.py")
        if "asyncio.get_event_loop(" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"use asyncio.get_running_loop() instead: {offenders}"


@pytest.mark.asyncio
async def test_the_shutdown_handler_takes_the_running_loop() -> None:
    """The lifespan installs the handlers from a running loop, so it must take that loop.

    ``asyncio.get_event_loop()`` returns the same loop only while one is running; once the implicit
    creation is removed, a call from a thread without a loop raises instead of returning one.
    """
    from fastapi import FastAPI

    from i3x_server.bootstrap.runtime_setup import install_shutdown_signal_handlers

    class _NoopSubscriptionService:
        def initiate_shutdown(self) -> None:
            return None

    running = asyncio.get_running_loop()
    handlers = install_shutdown_signal_handlers(FastAPI(), cast(SubscriptionService, _NoopSubscriptionService()))
    try:
        assert handlers.loop is running, "the handler record must hold the loop the lifespan is running on"
    finally:
        handlers.restore()
