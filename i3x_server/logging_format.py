"""Log formatting: plain text by default, JSON lines on request.

``I3X_LOG_FORMAT=json`` switches the root handler to one JSON object per line
(``timestamp``/``level``/``logger``/``message``, plus ``request_id`` and ``exception`` when the
record carries them). Plain text keeps the historical ``%(asctime)s %(levelname)s %(name)s
%(message)s`` layout and appends the request id when one is active, so a correlation id is
visible in either format without a structured-logging dependency.

The request id lives in a :class:`~contextvars.ContextVar`, which an asyncio task inherits from
the request middleware, so log lines emitted inside a handler - including SSE stream events -
carry the id of the request that produced them without every call site threading it through.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import datetime, timezone

LOG_FORMAT_TEXT = "text"
LOG_FORMAT_JSON = "json"
TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# Loggers whose own handlers uvicorn configured; they do not propagate to the root handler, so
# the chosen format is applied to them explicitly to keep container logs uniform.
_EXTERNAL_HANDLER_LOGGERS = ("uvicorn", "uvicorn.access", "uvicorn.error")

_request_id_var: ContextVar[str | None] = ContextVar("i3x_request_id", default=None)


def current_request_id() -> str | None:
    """The request id of the request currently being served, if any."""
    return _request_id_var.get()


@contextmanager
def request_id_context(request_id: str) -> Iterator[str]:
    """Bind ``request_id`` to the current task so log lines within the block carry it."""
    token: Token[str | None] = _request_id_var.set(request_id)
    try:
        yield request_id
    finally:
        _request_id_var.reset(token)


def _iso_utc(epoch: float) -> str:
    """UTC ISO-8601 timestamp with a ``Z`` suffix and fixed millisecond width.

    ``isoformat()`` drops the fraction when the microsecond is zero, which would make the field
    width vary line to line; a log shipper parsing this format wants a stable shape.
    """
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class TextFormatter(logging.Formatter):
    """Plain-text formatter that appends the active request id."""

    def __init__(self, fmt: str = TEXT_FORMAT) -> None:
        super().__init__(fmt=fmt)

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        request_id = current_request_id()
        if request_id is not None:
            rendered = f"{rendered} request_id={request_id}"
        return rendered


class JsonFormatter(logging.Formatter):
    """One JSON object per log record; exception text and request id are separate fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, str] = {
            "timestamp": _iso_utc(record.created),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = current_request_id()
        if request_id is not None:
            payload["request_id"] = request_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload)


def build_formatter(log_format: str) -> logging.Formatter:
    """Formatter for the requested format; anything but ``json`` is plain text."""
    if log_format.strip().lower() == LOG_FORMAT_JSON:
        return JsonFormatter()
    return TextFormatter()


def configure_logging(level_name: str, log_format: str) -> None:
    """Install the root handler for ``level_name`` and ``log_format``.

    ``force=True`` replaces handlers left over from a previous startup, which repeated
    ``create_app()`` calls (tests, reload) would otherwise accumulate. The asyncua logger is
    quieted while the connection supervisor keeps its lifecycle messages.
    """
    level = getattr(logging, level_name.strip().upper(), logging.INFO)
    formatter = build_formatter(log_format)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=[handler], force=True)
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    # Keep the connection supervisor's reconnect lifecycle messages visible.
    logging.getLogger("asyncua.client.client").setLevel(max(level, logging.INFO))
    # uvicorn configures its own handlers with propagate=False, so the format is applied there
    # too; otherwise access logs would stay plain text in a JSON deployment.
    for name in _EXTERNAL_HANDLER_LOGGERS:
        for external_handler in logging.getLogger(name).handlers:
            external_handler.setFormatter(formatter)
