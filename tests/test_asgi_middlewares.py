"""The hot path must be pure ASGI, and admission control must be measurable.

``app.middleware("http")`` is Starlette's ``BaseHTTPMiddleware`` in disguise: it runs each request
in a task group, copies the scope, and re-serialises every body chunk - overhead a gateway cannot
afford on the SSE-heavy routes. The two remaining layers here were added that way; GZip and CORS are
already pure ASGI in the vendored Starlette.

The metrics added alongside cover what the existing instruments cannot show: how many requests are
being served right now, how many admission control refused, how many client keys the limiter tracks,
and how long a streamed response keeps running after it started - the duration histogram is time to
first byte, so a stream that stays open for minutes looked instantaneous.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import MutableMapping
from time import perf_counter
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import generate_latest
from starlette.middleware.base import BaseHTTPMiddleware

from i3x_server.bootstrap import request_middlewares
from i3x_server.bootstrap.app_factory import create_app
from i3x_server.bootstrap.request_middlewares import (
    REQUEST_ID_HEADER,
    RequestObservabilityMiddleware,
    SecurityHeadersMiddleware,
)
from i3x_server.http_rate_limit import RateLimitMiddleware

_REQUEST_ID_SHAPE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_STREAM_COUNT = 'i3x_http_stream_duration_seconds_count{method="GET",path="/stream"}'
_STREAM_SUM = 'i3x_http_stream_duration_seconds_sum{method="GET",path="/stream"}'
_IN_FLIGHT = "i3x_http_requests_in_flight"
_SHED_RATE_LIMIT = 'i3x_http_requests_shed_total{reason="rate-limit"}'


def _registry_text() -> str:
    """The scrape body straight from the default registry, i.e. what a Prometheus poll would read."""
    return generate_latest().decode("utf-8")


def _metric_value(body: str, sample: str) -> float | None:
    """Value of a Prometheus sample line, or ``None`` when the line has never been observed."""
    for line in body.splitlines():
        if line.startswith(f"{sample} "):
            return float(line.rsplit(" ", 1)[1])
    return None


def _scope(path: str, headers: list[tuple[bytes, bytes]] | None = None) -> MutableMapping[str, Any]:
    """An HTTP scope with a matched route, so the metric label is the route template."""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "0.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [(b"host", b"testserver"), *(headers or [])],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "root_path": "",
        "state": {},
        "route": SimpleNamespace(path=path, path_format=path),
    }


def _receive(*messages: MutableMapping[str, Any]) -> Any:
    """A ``receive`` that hands out the given messages, then reports a disconnect."""
    remaining = list(messages)

    async def receive() -> MutableMapping[str, Any]:
        if remaining:
            return remaining.pop(0)
        return {"type": "http.disconnect"}

    return receive


def _collect(
    sent: list[MutableMapping[str, Any]],
    progress: asyncio.Event | None = None,
    at_least: int = 1,
) -> Any:
    """A ``send`` that records every message the stack emits, in the order it emitted them.

    ``progress`` is signalled once ``at_least`` messages have been collected, so a test can wait for
    a stream's first chunk without polling - that signal is what proves a layer is not buffering.
    """

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)
        if progress is not None and len(sent) >= at_least:
            progress.set()

    return send


async def _start_response(scope: MutableMapping[str, Any], receive: Any, send: Any, body: bytes = b"{}") -> None:
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body, "more_body": False})


def _status(sent: list[MutableMapping[str, Any]]) -> int | None:
    start = next((message for message in sent if message["type"] == "http.response.start"), None)
    return None if start is None else int(start["status"])


def _headers(sent: list[MutableMapping[str, Any]]) -> dict[str, str]:
    start = next(message for message in sent if message["type"] == "http.response.start")
    return {key.lower().decode(): value.decode() for key, value in start["headers"]}


def _middleware_classes(app: FastAPI) -> list[type]:
    """The middleware classes the app wraps, in the order Starlette stored them.

    Starlette types ``Middleware.cls`` as a factory callable, so the value is cast to a class: the
    assertion below is about the class itself, and ``issubclass`` needs a ``type``.
    """
    return [cast(type, middleware.cls) for middleware in app.user_middleware]


def test_the_added_middleware_layers_are_not_base_http_middleware() -> None:
    """``app.middleware("http")`` adds a ``BaseHTTPMiddleware``; the hot path must not carry one."""
    app = FastAPI()
    request_middlewares.add_security_headers(app, ("'sha256-abc'",))
    request_middlewares.add_request_observability(app)

    classes = _middleware_classes(app)
    assert classes, "the helpers must add their layers to the app"
    for cls in classes:
        assert not issubclass(cls, BaseHTTPMiddleware), f"{cls.__name__} buffers and re-serialises streamed responses"
    assert set(classes) == {SecurityHeadersMiddleware, RequestObservabilityMiddleware}


async def test_a_streamed_response_is_forwarded_chunk_by_chunk_and_measured() -> None:
    """The body must reach ``send`` before the handler finishes, and its duration must be recorded."""
    release = asyncio.Event()

    async def handler(scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        await release.wait()
        await send({"type": "http.response.body", "body": b"last", "more_body": False})

    sent: list[MutableMapping[str, Any]] = []
    first_chunk = asyncio.Event()
    served = asyncio.create_task(
        RequestObservabilityMiddleware(handler)(
            _scope("/stream"), _receive(), _collect(sent, progress=first_chunk, at_least=2)
        )
    )

    # A buffering layer would deliver nothing until the handler returns, so the response start and
    # the first chunk arriving while the handler is still blocked is what proves chunks pass through.
    await asyncio.wait_for(first_chunk.wait(), 5.0)
    assert _metric_value(_registry_text(), _STREAM_COUNT) is None, "the duration is recorded when the body completes"

    release.set()
    await served

    assert _status(sent) == 200
    assert _REQUEST_ID_SHAPE.fullmatch(_headers(sent)[REQUEST_ID_HEADER.lower()])
    assert _metric_value(_registry_text(), _STREAM_COUNT) == 1.0
    assert (_metric_value(_registry_text(), _STREAM_SUM) or 0.0) > 0.0


async def test_a_single_chunk_response_does_not_look_like_a_stream() -> None:
    """Only a body written in chunks extends past the response start, so the histogram stays honest."""
    sample = 'i3x_http_stream_duration_seconds_count{method="GET",path="/info"}'
    before = _metric_value(_registry_text(), sample)

    sent: list[MutableMapping[str, Any]] = []
    await RequestObservabilityMiddleware(_start_response)(_scope("/info"), _receive(), _collect(sent))

    assert _status(sent) == 200
    assert _metric_value(_registry_text(), sample) == before


async def test_in_flight_counts_concurrent_requests_and_clears_afterwards() -> None:
    """The gauge is the only way to see event-loop contention, so it must be live and must not leak."""
    arrived = 0
    all_arrived = asyncio.Event()
    release = asyncio.Event()

    async def handler(scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            all_arrived.set()
        await release.wait()
        await _start_response(scope, receive, send)

    served = asyncio.gather(
        RequestObservabilityMiddleware(handler)(_scope("/slow-a"), _receive(), _collect([])),
        RequestObservabilityMiddleware(handler)(_scope("/slow-b"), _receive(), _collect([])),
    )
    await asyncio.wait_for(all_arrived.wait(), 5.0)

    assert _metric_value(_registry_text(), _IN_FLIGHT) == 2.0
    release.set()
    await served

    assert _metric_value(_registry_text(), _IN_FLIGHT) == 0.0


async def test_an_aborted_request_clears_the_in_flight_gauge(caplog: pytest.LogCaptureFixture) -> None:
    """The abort path must release the gauge too, or a disconnect would stay in flight forever."""

    async def handler(scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        raise RuntimeError("connection dropped")

    sent: list[MutableMapping[str, Any]] = []
    with caplog.at_level(logging.INFO, logger="i3x_server.bootstrap.request_middlewares"):
        await RequestObservabilityMiddleware(handler)(_scope("/abort"), _receive(), _collect(sent))

    assert _status(sent) == 499
    assert _REQUEST_ID_SHAPE.fullmatch(_headers(sent)[REQUEST_ID_HEADER.lower()])
    assert any("HTTP request aborted by client" in item.getMessage() for item in caplog.records)
    assert _metric_value(_registry_text(), _IN_FLIGHT) == 0.0


async def test_a_failure_with_a_connected_peer_propagates_instead_of_hanging() -> None:
    """The disconnect probe must be bounded: a live peer has nothing to send.

    An unbounded ``receive`` would leave a failing request hanging until the client disconnected,
    which is worse than the error the handler raised.
    """

    async def handler(scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        raise RuntimeError("handler failed")

    async def connected_receive() -> MutableMapping[str, Any]:
        await asyncio.Event().wait()  # the peer stays connected and sends nothing
        raise AssertionError("unreachable")

    sent: list[MutableMapping[str, Any]] = []
    started = perf_counter()
    with pytest.raises(RuntimeError, match="handler failed"):
        await RequestObservabilityMiddleware(handler)(_scope("/broken"), connected_receive, _collect(sent))

    assert perf_counter() - started < 1.0, "the disconnect probe must not wait on a connected peer"
    assert not sent, "a handler failure with a live peer is not an abort, so nothing is answered here"
    assert _metric_value(_registry_text(), _IN_FLIGHT) == 0.0


async def test_an_app_that_never_started_a_response_is_counted_as_500() -> None:
    """Nothing was delivered, so the dashboard must see a failure rather than a silent success."""

    async def handler(scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        return None

    sent: list[MutableMapping[str, Any]] = []
    await RequestObservabilityMiddleware(handler)(_scope("/silent"), _receive(), _collect(sent))

    assert not sent
    assert _metric_value(_registry_text(), 'i3x_http_requests_total{method="GET",path="/silent",status="500"}') == 1.0
    assert _metric_value(_registry_text(), _IN_FLIGHT) == 0.0


async def test_a_referrer_policy_the_handler_set_is_kept() -> None:
    """The ``setdefault`` semantics apply to every baseline header, not only the content-type one."""

    async def handler(scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"referrer-policy", b"strict-origin-when-cross-origin")],
            }
        )
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    sent: list[MutableMapping[str, Any]] = []
    await SecurityHeadersMiddleware(handler, ("'sha256-abc'",))(_scope("/v1/info"), _receive(), _collect(sent))

    headers = _headers(sent)
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert headers["x-content-type-options"] == "nosniff"


async def test_a_cancelled_request_records_an_abort_and_propagates_the_cancellation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The server's cancellation must survive the middleware, and must not leak the gauge.

    Swallowing ``CancelledError`` would make a shutdown look like a completed request, so the abort
    is recorded and the cancellation re-raised. A cancelled task cannot deliver a response, so
    nothing is sent - the ``499`` is the accounting, not a reply.
    """
    entered = asyncio.Event()

    async def handler(scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        entered.set()
        await asyncio.sleep(30)

    sent: list[MutableMapping[str, Any]] = []
    served = asyncio.create_task(RequestObservabilityMiddleware(handler)(_scope("/cancel"), _receive(), _collect(sent)))
    await asyncio.wait_for(entered.wait(), 5.0)
    assert _metric_value(_registry_text(), _IN_FLIGHT) == 1.0

    served.cancel()
    with caplog.at_level(logging.INFO, logger="i3x_server.bootstrap.request_middlewares"):
        with pytest.raises(asyncio.CancelledError):
            await served

    assert not sent, "a cancelled task must not answer"
    assert any("HTTP request aborted by client" in item.getMessage() for item in caplog.records)
    assert _metric_value(_registry_text(), 'i3x_http_requests_total{method="GET",path="/cancel",status="499"}') == 1.0
    assert _metric_value(_registry_text(), _IN_FLIGHT) == 0.0


async def test_a_stream_that_broke_after_starting_records_the_status_it_announced() -> None:
    """A half-delivered response served a status, so it must not be counted as a 499 abort."""

    async def handler(scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b"event: ping\n", "more_body": True})
        raise RuntimeError("upstream dropped")

    sent: list[MutableMapping[str, Any]] = []
    with pytest.raises(RuntimeError, match="upstream dropped"):
        await RequestObservabilityMiddleware(handler)(_scope("/sse"), _receive(), _collect(sent))

    assert _status(sent) == 200
    assert _metric_value(_registry_text(), 'i3x_http_requests_total{method="GET",path="/sse",status="200"}') == 1.0
    assert (
        _metric_value(_registry_text(), 'i3x_http_stream_duration_seconds_count{method="GET",path="/sse"}') is None
    ), "a body that never finished has no duration to report"
    assert _metric_value(_registry_text(), _IN_FLIGHT) == 0.0


async def test_a_cancelled_stream_keeps_the_status_it_announced() -> None:
    """Cancellation after the response started still records what the client received."""
    entered = asyncio.Event()

    async def handler(scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"chunk", "more_body": True})
        entered.set()
        await asyncio.sleep(30)

    sent: list[MutableMapping[str, Any]] = []
    served = asyncio.create_task(
        RequestObservabilityMiddleware(handler)(_scope("/sse-cancel"), _receive(), _collect(sent))
    )
    await asyncio.wait_for(entered.wait(), 5.0)
    served.cancel()
    with pytest.raises(asyncio.CancelledError):
        await served

    assert _status(sent) == 200
    assert (
        _metric_value(_registry_text(), 'i3x_http_requests_total{method="GET",path="/sse-cancel",status="200"}') == 1.0
    )
    assert (
        _metric_value(_registry_text(), 'i3x_http_stream_duration_seconds_count{method="GET",path="/sse-cancel"}')
        is None
    ), "a cancelled stream never completed its body"
    assert _metric_value(_registry_text(), _IN_FLIGHT) == 0.0


async def test_shed_requests_are_counted_with_the_reason_that_refused_them() -> None:
    """A tripped limit is admission control, not a handler failure, so it must be attributable."""
    before = _metric_value(_registry_text(), _SHED_RATE_LIMIT) or 0.0

    limiter = RateLimitMiddleware(_start_response, limit_per_minute=1)
    admitted: list[MutableMapping[str, Any]] = []
    await limiter(_scope("/one"), _receive(), _collect(admitted))
    shed: list[MutableMapping[str, Any]] = []
    await limiter(_scope("/two"), _receive(), _collect(shed))

    assert _status(admitted) == 200
    assert _status(shed) == 429
    assert _metric_value(_registry_text(), _SHED_RATE_LIMIT) == before + 1.0
    assert _metric_value(_registry_text(), "i3x_http_rate_limit_clients") == 1.0


async def test_security_headers_are_added_without_buffering_the_response() -> None:
    """The rewritten layer must keep the header behaviour, including leaving a set header alone."""

    async def handler(scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json"), (b"x-content-type-options", b"nosniff")],
            }
        )
        await send({"type": "http.response.body", "body": b"{}", "more_body": False})

    sent: list[MutableMapping[str, Any]] = []
    await SecurityHeadersMiddleware(handler, ("'sha256-abc'",))(_scope("/v1/info"), _receive(), _collect(sent))

    headers = _headers(sent)
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert "content-security-policy" not in headers, "only HTML responses get a CSP"


def test_the_assembled_app_carries_no_base_http_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every layer of the assembled stack must be pure ASGI, including the transport ones."""
    monkeypatch.setenv("I3X_SKIP_OPCUA_CONNECT", "1")
    monkeypatch.setenv("I3X_ENABLE_MCP", "0")

    app = create_app()
    for cls in _middleware_classes(app):
        assert not issubclass(cls, BaseHTTPMiddleware), (
            f"{cls.__name__} sits on the hot path and buffers streamed responses"
        )


def test_the_correlation_id_is_echoed_by_the_rewritten_layer(client: TestClient) -> None:
    """Regression: an ASGI layer must echo the id on the response start, as the old one did."""
    response = client.get("/health", headers={REQUEST_ID_HEADER: "corr-3"})
    assert response.headers[REQUEST_ID_HEADER] == "corr-3"
