"""Opt-in per-client request rate limiting.

The limiter is a gateway concern, not part of the i3X contract: the spec defines no admission
semantics, so tripping the limit answers HTTP 429 with the spec-neutral error envelope the
gateway uses for every other failure. It is disabled by default
(``I3X_REQUEST_RATE_LIMIT_PER_MINUTE=0``) and keys on the connecting IP; behind a reverse proxy
that is the proxy's IP, so deployments behind one must limit at the proxy instead.

A sliding window is used rather than a fixed bucket: a burst that straddles a minute boundary
cannot double the effective rate, which is what an upstream-DoS guard needs to bound.
"""

from __future__ import annotations

import json
import time
from collections import deque
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

WINDOW_SECONDS = 60.0
# Distinct client keys are bounded so a spoofed stream of source addresses cannot turn the
# limiter itself into a memory leak; the least recently admitted key is evicted.
_MAX_CLIENT_KEYS = 10_000


def _rate_limit_payload(limit_per_minute: int) -> dict[str, Any]:
    """The gateway's error envelope for a tripped limit (same shape as ``i3x_http_error``)."""
    message = "Rate limit exceeded"
    detail = f"{message} (limit={limit_per_minute}/minute, window={int(WINDOW_SECONDS)}s)"
    return {
        "success": False,
        "error": {"code": 429, "message": message},
        "responseDetail": {
            "title": "Too Many Requests",
            "status": 429,
            "detail": detail,
        },
    }


def _client_key(scope: Scope) -> str:
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        return str(client[0])
    return "unknown"


class RateLimitMiddleware:
    """Pure ASGI middleware so SSE streams stay unbuffered and request bodies are never read."""

    def __init__(self, app: ASGIApp, limit_per_minute: int) -> None:
        self.app = app
        self.limit_per_minute = max(1, int(limit_per_minute))
        self._hits_by_client: dict[str, deque[float]] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # HTTP only: a websocket scope cannot be answered with an http.response message, and the
        # gateway's only websocket surface (SSE uses http) is already covered. Lifespan and other
        # internal scopes must pass through untouched.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        client_key = _client_key(scope)
        if not self._admit(client_key):
            await self._send_rate_limit(send, client_key)
            return

        await self.app(scope, receive, send)

    def _admit(self, client_key: str) -> bool:
        """Record one hit for ``client_key``; return ``False`` when the window is full.

        A rejected request is not recorded, so a client hammering the limit cannot slide the
        window forward and starve itself indefinitely.
        """
        now = time.monotonic()
        hits = self._hits_by_client.get(client_key)
        if hits is not None:
            while hits and now - hits[0] >= WINDOW_SECONDS:
                hits.popleft()
            if not hits:
                del self._hits_by_client[client_key]
                hits = None
        if hits is None:
            if len(self._hits_by_client) >= _MAX_CLIENT_KEYS:
                # Evict the key whose newest hit is oldest: that one is idle the longest.
                stale_key = min(self._hits_by_client, key=lambda key: self._hits_by_client[key][-1])
                del self._hits_by_client[stale_key]
            hits = deque()
            self._hits_by_client[client_key] = hits
        if len(hits) >= self.limit_per_minute:
            return False
        hits.append(now)
        return True

    def retry_after_seconds(self, client_key: str) -> int:
        """Seconds until the oldest hit of ``client_key`` ages out (1 when nothing is pending)."""
        hits = self._hits_by_client.get(client_key)
        if not hits:
            return 1
        return max(1, int(WINDOW_SECONDS - (time.monotonic() - hits[0]) + 1))

    async def _send_rate_limit(self, send: Send, client_key: str) -> None:
        body = json.dumps(_rate_limit_payload(self.limit_per_minute)).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"retry-after", str(self.retry_after_seconds(client_key)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})
