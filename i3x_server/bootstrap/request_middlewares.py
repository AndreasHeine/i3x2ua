"""HTTP middlewares assembled by ``create_app``.

Middleware order is meaningful in Starlette (the last added is the outermost), so the whole
stack stays in one place and ``create_app`` adds the layers in the documented order.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

from i3x_server import logging_format
from i3x_server.application.state import gateway_state
from i3x_server.config.settings import get_settings, settings
from i3x_server.http_caching import MODEL_REVISION_HEADER, ModelRevisionMiddleware
from i3x_server.http_metrics import observe_request, route_label
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
    rate_limit_per_minute = get_settings().request_rate_limit_per_minute
    if rate_limit_per_minute > 0:
        app.add_middleware(RateLimitMiddleware, limit_per_minute=rate_limit_per_minute)
    cors_origins = settings.cors_allowed_origins
    # MCP origin allowlist: dedicated origins win, otherwise the CORS allowlist (without the
    # wildcard) is the source of truth. Wildcards are never a usable comparison target. The
    # values are read from the current environment (like the rate-limit toggle), not from the
    # import-time snapshot, so the setting behaves like every other documented env knob.
    origin_settings = get_settings()
    mcp_origin_sources = origin_settings.mcp_allowed_origins or origin_settings.cors_allowed_origins
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


def add_security_headers(app: FastAPI, frontend_script_hashes: tuple[str, ...]) -> None:
    """Baseline security headers, plus a CSP that differs between docs pages and UI pages.

    The docs pages need the CDN assets FastAPI's Swagger/ReDoc reference; the UI pages get a
    stricter policy that allows only the inline scripts hashed from the prerendered bundle.
    """

    @app.middleware("http")
    async def add_security_headers_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if response.headers.get("content-type", "").startswith("text/html"):
            is_docs_route = request.url.path.startswith("/docs") or request.url.path.startswith("/redoc")
            csp_value = (
                "default-src 'self'; img-src 'self' data: https://fastapi.tiangolo.com; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
                if is_docs_route
                else "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                f"script-src 'self' {' '.join(frontend_script_hashes)}; connect-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'"
            )
            response.headers.setdefault(
                "Content-Security-Policy",
                csp_value,
            )
        return response


def add_request_observability(app: FastAPI) -> None:
    """Own one correlation id, one log line, and one metric observation per request.

    The id is resolved before the request runs and bound to the task (see
    :mod:`i3x_server.logging_format`), so log lines emitted inside a handler - including SSE
    stream events - carry it, and the response echoes it in ``X-Request-ID``.

    An aborted request (client disconnect mid-stream) surfaces as a ``RuntimeError`` with no
    response; it is a client-side cancellation, not a gateway failure, so it is logged as an
    informational abort and counted as ``499`` rather than reported as an unhandled exception.

    Request duration is measured until the response *starts*: a streamed body keeps flowing
    after this middleware returns, so the value is time to first byte, which is what
    :mod:`i3x_server.http_metrics` documents. Recording metrics here avoids a second middleware
    layer, since the duration is measured for the log line anyway.
    """

    @app.middleware("http")
    async def observe_http_request(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = resolve_request_id(request.headers.get(REQUEST_ID_HEADER))
        started = perf_counter()
        with logging_format.request_id_context(request_id):
            try:
                response = await call_next(request)
            except RuntimeError as exc:
                if str(exc) == "No response returned." and await request.is_disconnected():
                    duration_s = perf_counter() - started
                    logger.info(
                        "HTTP request aborted by client method=%s path=%s duration_s=%.3f",
                        request.method,
                        request.url.path,
                        duration_s,
                    )
                    # The route is resolved once the request has been routed, which is what keeps
                    # the label a template instead of one label per resource.
                    observe_request(request.method, route_label(request.scope), 499, duration_s)
                    return Response(status_code=499, headers={REQUEST_ID_HEADER: request_id})
                raise
            duration_s = perf_counter() - started
            logger.info(
                "HTTP request method=%s path=%s status=%s duration_s=%.3f",
                request.method,
                request.url.path,
                response.status_code,
                duration_s,
            )
            observe_request(request.method, route_label(request.scope), response.status_code, duration_s)
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
