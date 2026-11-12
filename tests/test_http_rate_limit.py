"""Per-client request rate limiting (gateway admission control)."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from starlette.types import Receive, Scope, Send

from i3x_server.bootstrap import create_app
from i3x_server.http_rate_limit import WINDOW_SECONDS, RateLimitMiddleware
from tests.conftest import _client_with_env


async def _noop_app(scope: Scope, receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"ok", "more_body": False})


async def _call(middleware: RateLimitMiddleware, client_host: str) -> tuple[int, dict[str, str], str]:
    sent: list[Any] = []

    async def send(message: Any) -> None:
        sent.append(message)

    async def receive() -> Any:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": "/v1/info",
        "headers": [],
        "client": (client_host, 50000),
    }
    await middleware(scope, receive, send)

    start = next(message for message in sent if message["type"] == "http.response.start")
    headers = {name.decode("ascii").lower(): value.decode("ascii") for name, value in start["headers"]}
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return int(start["status"]), headers, body.decode("utf-8")


def test_rate_limit_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """No limiter middleware unless I3X_REQUEST_RATE_LIMIT_PER_MINUTE is set (spec-neutral default)."""
    monkeypatch.setenv("I3X_REQUEST_RATE_LIMIT_PER_MINUTE", "0")
    app = create_app()
    assert not any(middleware.cls is RateLimitMiddleware for middleware in app.user_middleware)


def test_rate_limit_middleware_is_installed_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("I3X_REQUEST_RATE_LIMIT_PER_MINUTE", "30")
    monkeypatch.setenv("I3X_CORS_ALLOWED_ORIGINS", '["https://example.test"]')
    app = create_app()
    limiter = next(middleware for middleware in app.user_middleware if middleware.cls is RateLimitMiddleware)
    assert limiter.kwargs == {"limit_per_minute": 30}
    # CORS stays outermost so a tripped limit still carries the CORS headers.
    assert app.user_middleware.index(limiter) > next(
        index for index, middleware in enumerate(app.user_middleware) if middleware.cls.__name__ == "CORSMiddleware"
    )


@pytest.mark.asyncio
async def test_rate_limit_rejects_only_the_over_limit_client() -> None:
    middleware = RateLimitMiddleware(_noop_app, limit_per_minute=2)

    assert await _call(middleware, "203.0.113.1") == (200, {"content-type": "text/plain"}, "ok")
    assert await _call(middleware, "203.0.113.1") == (200, {"content-type": "text/plain"}, "ok")

    status, headers, body = await _call(middleware, "203.0.113.1")
    assert status == 429
    assert headers["content-type"] == "application/json"
    assert headers["retry-after"]
    payload = json.loads(body)
    assert payload["success"] is False
    assert payload["error"] == {"code": 429, "message": "Rate limit exceeded"}
    assert payload["responseDetail"]["status"] == 429
    assert payload["responseDetail"]["title"] == "Too Many Requests"
    assert "limit=2/minute" in payload["responseDetail"]["detail"]

    # A different client is unaffected, and a rejected request must not extend its own window.
    assert await _call(middleware, "203.0.113.2") == (200, {"content-type": "text/plain"}, "ok")
    assert len(middleware._hits_by_client["203.0.113.1"]) == 2


@pytest.mark.asyncio
async def test_rate_limit_window_slides_instead_of_resetting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Aged-out hits free the window; a client that is still hammering stays rejected."""
    middleware = RateLimitMiddleware(_noop_app, limit_per_minute=2)
    now = time.monotonic()

    def fake_monotonic() -> float:
        return now

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    await _call(middleware, "198.51.100.7")
    await _call(middleware, "198.51.100.7")
    assert (await _call(middleware, "198.51.100.7"))[0] == 429

    # Fixed buckets would reset at the minute boundary and allow a second full burst; the sliding
    # window only frees the slots whose hits aged out.
    now += WINDOW_SECONDS - 1
    assert (await _call(middleware, "198.51.100.7"))[0] == 429
    now += 2
    assert (await _call(middleware, "198.51.100.7"))[0] == 200


@pytest.mark.asyncio
async def test_rate_limit_bounds_client_key_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spoofed source addresses must not grow the limiter without bound."""
    monkeypatch.setattr("i3x_server.http_rate_limit._MAX_CLIENT_KEYS", 3)
    middleware = RateLimitMiddleware(_noop_app, limit_per_minute=1)

    for host in ("10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"):
        assert (await _call(middleware, host))[0] == 200

    assert len(middleware._hits_by_client) == 3
    assert "10.0.0.1" not in middleware._hits_by_client
    assert "10.0.0.4" in middleware._hits_by_client


@pytest.mark.asyncio
async def test_rate_limit_skips_non_http_scopes() -> None:
    """Lifespan/internal scopes must not be counted or answered."""
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])
        await _noop_app(scope, receive, send)

    middleware = RateLimitMiddleware(app, limit_per_minute=1)
    for scope_type in ("lifespan", "websocket"):
        messages: list[Any] = []

        # Default arguments bind the current iteration; closures that read the loop variable
        # would see the final value (ruff B023).
        async def send(message: Any, sink: list[Any] = messages) -> None:
            sink.append(message)

        async def receive(current_scope_type: str = scope_type) -> Any:
            return {"type": "lifespan.startup"} if current_scope_type == "lifespan" else {"type": "websocket.connect"}

        await middleware({"type": scope_type, "headers": [], "client": ("10.0.0.9", 1)}, receive, send)
        assert messages[0]["status"] == 200
    assert seen == ["lifespan", "websocket"]
    assert middleware._hits_by_client == {}


def test_rate_limit_applies_to_the_gateway_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    """Integration: the limiter is wired into the real app and answers the error envelope."""
    with _client_with_env(monkeypatch, {"I3X_REQUEST_RATE_LIMIT_PER_MINUTE": "3"}) as client:
        for _ in range(3):
            assert client.post("/v1/subscriptions", json={"clientId": "limited-client"}).status_code == 200

        limited = client.post("/v1/subscriptions", json={"clientId": "limited-client"})
        assert limited.status_code == 429
        payload = limited.json()
        assert payload["success"] is False
        assert payload["error"]["code"] == 429
        assert payload["responseDetail"]["title"] == "Too Many Requests"


def test_rate_limit_covers_static_and_diagnostic_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The limiter is a gateway concern, so it guards non-spec routes too."""
    with _client_with_env(monkeypatch, {"I3X_REQUEST_RATE_LIMIT_PER_MINUTE": "1"}) as client:
        assert client.get("/ua/connection").status_code == 200
        assert client.get("/ua/connection").status_code == 429
