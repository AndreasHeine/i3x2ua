"""HTTP middlewares assembled by ``create_app``.

Middleware order is meaningful in Starlette (the last added is the outermost), so the whole
stack stays in one place and ``create_app`` adds the layers in the documented order. Every layer
is pure ASGI (``__call__(scope, receive, send)``): none of them copies the scope or re-serialises
body chunks, so streamed responses reach the client chunk by chunk.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from i3x_server import logging_format
from i3x_server.application.state import gateway_state
from i3x_server.config.settings import get_settings
from i3x_server.http_caching import MODEL_REVISION_HEADER, ModelRevisionMiddleware
from i3x_server.http_metrics import observe_request, observe_stream, requests_in_flight, route_label
from i3x_server.http_rate_limit import RateLimitMiddleware

logger = logging.getLogger(__name__)

# Correlation header the gateway echoes on every response. Names are gateway-side, not part of
# the i3X contract.
REQUEST_ID_HEADER = "X-Request-ID"
# The header is attacker-controlled, so only a bounded, printable token is reused: newlines
# would let a caller forge log lines, and an unbounded value would land in every log record.
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}")


def resolve_request_id(incoming: str | None) -> str:
    """Reuse a safe client-supplied request id, or generate one.

    Anything outside the safe token pattern (wrong characters, longer than 128, empty) is
    replaced by a generated id rather than echoed back or logged verbatim.
    """
    if incoming is not None and _REQUEST_ID_PATTERN.fullmatch(incoming):
        return incoming
    return uuid4().hex


def add_transport_middlewares(app: FastAPI) -> None:
    """Response encoding, admission control, and CORS.

    ``ModelRevisionMiddleware`` is added first so it is the innermost middleware and sees
    uncompressed responses. CORS is added last so it stays outermost: a tripped rate limit must
    still carry the CORS headers a browser needs to read the 429 body.
    """
    state = gateway_state(app)
    app.add_middleware(ModelRevisionMiddleware)
    # Spec CORE-04 / Implementation Guide § Transport & Encoding: any response to a request
    # with Accept-Encoding: gzip must carry Content-Encoding: gzip, so compression must apply
    # even to small bodies (minimum_size=1).
    app.add_middleware(GZipMiddleware, minimum_size=1)
    # Opt-in admission control (see ``http_rate_limit``). ``get_settings()`` reads the current
    # environment, so the toggle behaves like every other documented env knob.
    runtime = get_settings()
    rate_limit_per_minute = runtime.request_rate_limit_per_minute
    if rate_limit_per_minute > 0:
        app.add_middleware(RateLimitMiddleware, limit_per_minute=rate_limit_per_minute)
    # MCP stream admission lives on the state container because the counter is per connecting
    # address and spans requests; the limit itself is read from the current environment, like the
    # origin allowlist below.
    state.mcp_max_sse_streams_per_client = runtime.mcp_max_sse_streams_per_client
    cors_origins = runtime.cors_allowed_origins
    # MCP origin allowlist: dedicated origins win, otherwise the CORS allowlist (without the
    # wildcard) is the source of truth. Wildcards are never a usable comparison target. The
    # values are read from the current environment (like the rate-limit toggle), not from the
    # import-time snapshot, so the setting behaves like every other documented env knob.
    mcp_origin_sources = runtime.mcp_allowed_origins or cors_origins
    state.mcp_allowed_origins = [origin for origin in mcp_origin_sources if origin != "*"]
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "Accept", "Authorization", "If-None-Match", "MCP-Protocol-Version"],
            # The correlation id is echoed on every response, so a browser client behind a CORS
            # boundary must be able to read it.
            expose_headers=["ETag", MODEL_REVISION_HEADER, REQUEST_ID_HEADER],
            allow_credentials=False,
        )


class SecurityHeadersMiddleware:
    """Pure ASGI middleware for the baseline security headers, plus a CSP that differs between
    docs pages and UI pages.

    ``app.middleware("http")`` is Starlette's ``BaseHTTPMiddleware`` in disguise: it copies the
    scope, runs the handler in a task group, and re-serialises every body chunk, which buffers the
    streamed routes this gateway is built around. These headers belong to the
    ``http.response.start`` message, so the layer edits that message and passes everything else
    through untouched.
    """

    def __init__(self, app: ASGIApp, frontend_script_hashes: Sequence[str]) -> None:
        self.app = app
        # The docs pages need the CDN assets FastAPI's Swagger/ReDoc reference; the UI pages get a
        # stricter policy that allows only the inline scripts hashed from the prerendered bundle.
        self._docs_csp = (
            "default-src 'self'; img-src 'self' data: https://fastapi.tiangolo.com; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        self._ui_csp = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            f"script-src 'self' {' '.join(frontend_script_hashes)}; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                # ``setdefault`` semantics: a value the handler set itself is kept.
                if "x-content-type-options" not in headers:
                    headers["x-content-type-options"] = "nosniff"
                if "referrer-policy" not in headers:
                    headers["referrer-policy"] = "no-referrer"
                if headers.get("content-type", "").startswith("text/html") and "content-security-policy" not in headers:
                    path = scope["path"]
                    is_docs_route = path.startswith("/docs") or path.startswith("/redoc")
                    headers["content-security-policy"] = self._docs_csp if is_docs_route else self._ui_csp
            await send(message)

        await self.app(scope, receive, send_with_security_headers)


def add_security_headers(app: FastAPI, frontend_script_hashes: tuple[str, ...]) -> None:
    """Add :class:`SecurityHeadersMiddleware` to the stack."""
    app.add_middleware(SecurityHeadersMiddleware, frontend_script_hashes=frontend_script_hashes)


class RequestObservabilityMiddleware:
    """Pure ASGI middleware that owns one correlation id, one log line, and one metric set per request.

    The id is resolved before the request runs and bound to the task (see
    :mod:`i3x_server.logging_format`), so log lines emitted inside a handler - including SSE stream
    events - carry it, and the response echoes it in ``X-Request-ID``.

    An aborted request (client disconnect mid-response) is a client-side cancellation, not a gateway
    failure: it is logged as an informational abort and counted as ``499``. The disconnect is read
    from the ASGI messages rather than from a framework's error text, so any exception raised while
    the peer is gone takes the abort path - not just one phrasing of ``RuntimeError``. A task the
    *server* cancelled (shutdown) is recorded the same way, but the cancellation is re-raised: a
    middleware that swallowed it would hide the shutdown from the server.

    Duration is measured until the response *starts* - a streamed body keeps flowing after that, so
    ``observe_request`` records time to first byte, while a body written in chunks also reports how
    long it ran overall (:func:`i3x_server.http_metrics.observe_stream`). Counting the request in
    flight (:data:`i3x_server.http_metrics.requests_in_flight`) is what makes contention on a single
    uvicorn worker visible. A request the app served without ever starting a response is reported as
    ``500``: nothing was delivered, and the difference between ``0`` and ``500`` is not a fact a
    dashboard can use.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = resolve_request_id(Headers(scope=scope).get(REQUEST_ID_HEADER))
        started = perf_counter()
        response_status: int | None = None
        response_started = False
        streamed = False
        body_finished: float | None = None
        saw_disconnect = False

        async def receive_observed() -> Message:
            # The handler reads the body through this wrapper, so a disconnect it consumed is
            # recorded without consuming anything extra.
            nonlocal saw_disconnect
            message = await receive()
            if message["type"] == "http.disconnect":
                saw_disconnect = True
            return message

        async def send_observed(message: Message) -> None:
            nonlocal response_status, response_started, streamed, body_finished
            if message["type"] == "http.response.start":
                response_started = True
                response_status = int(message["status"])
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            elif message["type"] == "http.response.body":
                if message.get("more_body", False):
                    streamed = True
                else:
                    body_finished = perf_counter()
            await send(message)

        with requests_in_flight.track_inprogress():
            # The log line and the observations are emitted inside the correlation context, so the
            # request id lands on the record whether the formatter is text or JSON.
            with logging_format.request_id_context(request_id):
                try:
                    await self.app(scope, receive_observed, send_observed)
                except asyncio.CancelledError:
                    # The server cancelled the task - shutdown, or a disconnect it observed itself.
                    # The request is recorded and the cancellation is propagated: swallowing it
                    # would hide the shutdown from the server. Nothing is sent, because a cancelled
                    # task cannot deliver a response.
                    _record_failure(
                        scope,
                        perf_counter() - started,
                        response_started,
                        response_status,
                        streamed,
                        body_finished,
                        started,
                    )
                    raise

                except Exception:
                    duration_s = perf_counter() - started
                    if not response_started and await _peer_is_gone(saw_disconnect, receive_observed):
                        # An abort is answered with 499 so the status the client sees matches the
                        # one the dashboard counts. No response had started, so nothing is overwritten.
                        _record_failure(
                            scope,
                            duration_s,
                            response_started,
                            response_status,
                            streamed,
                            body_finished,
                            started,
                        )
                        await send_observed({"type": "http.response.start", "status": 499, "headers": []})
                        await send_observed({"type": "http.response.body", "body": b"", "more_body": False})
                        return

                    if response_started and response_status is not None:
                        # The response had started, so whatever broke the stream still served a status.
                        _record_request(scope, response_status, duration_s, streamed, body_finished, started)
                    raise

                duration_s = perf_counter() - started
                _record_request(
                    scope,
                    response_status if response_status is not None else 500,
                    duration_s,
                    streamed,
                    body_finished,
                    started,
                )


# How long the disconnect probe waits for a message. A peer that is still connected has nothing to
# send, so an unbounded read would hang a request that is already failing; the probe only ever runs
# on that path, so a short bound is enough to tell "gone" from "still there".
_PEER_PROBE_TIMEOUT_SECONDS = 0.05


async def _peer_is_gone(saw_disconnect: bool, receive: Receive) -> bool:
    """Whether the peer went away: a disconnect the handler consumed, or one this probe reads.

    Only reached before a response has started, so reading ``receive`` cannot take a body chunk a
    handler still needs.
    """
    if saw_disconnect:
        return True
    try:
        message = await asyncio.wait_for(receive(), _PEER_PROBE_TIMEOUT_SECONDS)
    except TimeoutError:
        return False
    message_type: object = message["type"]
    return message_type == "http.disconnect"


def _record_request(
    scope: Scope,
    status: int,
    duration_s: float,
    streamed: bool,
    body_finished: float | None,
    started: float,
) -> None:
    """Emit the one log line and the metric observations a served request produces."""
    logger.info(
        "HTTP request method=%s path=%s status=%s duration_s=%.3f",
        scope["method"],
        scope["path"],
        status,
        duration_s,
    )
    label = route_label(scope)
    observe_request(scope["method"], label, status, duration_s)
    if streamed and body_finished is not None:
        observe_stream(scope["method"], label, body_finished - started)


def _record_failure(
    scope: Scope,
    duration_s: float,
    response_started: bool,
    response_status: int | None,
    streamed: bool,
    body_finished: float | None,
    started: float,
) -> None:
    """Record a request that broke before its body finished.

    Nothing served yet means the peer never saw a response, so it is counted as a ``499`` abort -
    the same accounting as a client disconnect - while a response that had already started keeps the
    status it announced, because that is what the client received.
    """
    if response_started and response_status is not None:
        _record_request(scope, response_status, duration_s, streamed, body_finished, started)
        return

    logger.info(
        "HTTP request aborted by client method=%s path=%s duration_s=%.3f",
        scope["method"],
        scope["path"],
        duration_s,
    )
    # The route is resolved once the request has been routed, which is what keeps
    # the label a template instead of one label per resource.
    observe_request(scope["method"], route_label(scope), 499, duration_s)


def add_request_observability(app: FastAPI) -> None:
    """Add :class:`RequestObservabilityMiddleware` to the stack."""
    app.add_middleware(RequestObservabilityMiddleware)
