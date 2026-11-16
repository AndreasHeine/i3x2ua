"""Long-lived background tasks report their own failure.

``asyncio.create_task`` hands back a handle, but a loop that ended early logged nothing: its
exception was only printed by asyncio when the interpreter collected the task, while the work the
loop owned - subscription expiry, connection-state mirroring, an OPC UA metadata refresh - stopped
for the rest of the process. Every long-lived task is started through this helper so the handle is
kept and an unexpected failure is reported once, attributed to the module that owns the task.

A cancellation is not a failure. Shutdown cancels these loops on purpose, so a cancelled task is
reported nowhere and stays a cancellation for whoever awaits it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


def start_tracked_task(
    coro: Coroutine[Any, Any, T],
    description: str,
    logger: logging.Logger,
) -> asyncio.Task[T]:
    """Start ``coro`` as a task and report an unexpected failure when it completes.

    ``description`` names the loop in the log line and ``logger`` is the logger of the owning
    module, so the record appears under that module rather than under this helper. The caller keeps
    the returned handle; the helper does not cancel or restart anything.
    """
    task = asyncio.create_task(coro)
    task.add_done_callback(_failure_reporter(description, logger))
    return task


def _failure_reporter(description: str, logger: logging.Logger) -> Callable[[asyncio.Task[Any]], None]:
    """Build the done callback that reports a failure and retrieves its exception.

    Reading ``exception()`` is the part that keeps asyncio from printing ``Task exception was never
    retrieved`` later: fire-and-forget callers never await the task, so nobody else reads it.
    """

    def _report(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            logger.error("Background task failed task=%s error=%s", description, exception, exc_info=exception)

    return _report
