"""Request-id correlation, JSON log rendering, and the Prometheus scrape endpoint.

These are gateway-side observability features, so the assertions cover both directions: what is
echoed and counted, and what is *refused* (an unsafe correlation header, a diagnostics read that
fails, an unknown log format).
"""

from __future__ import annotations

import io
import json
import logging
import re
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import generate_latest
from pydantic import ValidationError

from i3x_server import logging_format
from i3x_server.application.state import gateway_state
from i3x_server.bootstrap import request_middlewares
from i3x_server.bootstrap.app_factory import create_app
from i3x_server.bootstrap.request_middlewares import REQUEST_ID_HEADER, resolve_request_id
from i3x_server.config.settings import Settings
from i3x_server.http_metrics import UNMATCHED_PATH, route_label

_REQUEST_ID_SHAPE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
# One line per served request: the message is identical in text and JSON format, and the
# correlation id is added by the formatter rather than baked into it.
_REQUEST_LOG_MESSAGE = re.compile(r"HTTP request method=GET path=/health status=200 duration_s=\d+\.\d{3}")


def _metric_value(body: str, sample: str) -> float | None:
    """Value of a Prometheus sample line, or ``None`` when the line is absent."""
    for line in body.splitlines():
        if line.startswith(f"{sample} "):
            return float(line.rsplit(" ", 1)[1])
    return None


def _registry_text() -> str:
    """The scrape body straight from the registry, i.e. what a Prometheus poll would read."""
    return generate_latest().decode("utf-8")


@pytest.fixture
def restored_logging() -> Iterator[None]:
    """Restore the root logger handlers and level after a test reconfigures logging."""
    handlers = logging.root.handlers[:]
    level = logging.root.level
    yield
    logging.root.handlers = handlers
    logging.root.setLevel(level)


def test_generated_request_id_is_echoed_on_every_response(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    request_id = response.headers[REQUEST_ID_HEADER]
    assert _REQUEST_ID_SHAPE.fullmatch(request_id)


def test_client_supplied_request_id_is_propagated(client: TestClient) -> None:
    response = client.get("/health", headers={REQUEST_ID_HEADER: "trace-42"})
    assert response.headers[REQUEST_ID_HEADER] == "trace-42"


@pytest.mark.parametrize(
    "unsafe_value",
    ["", "has space", "newline\ninjected", "x" * 129],
)
def test_unsafe_request_id_header_is_replaced(client: TestClient, unsafe_value: str) -> None:
    response = client.get("/health", headers={REQUEST_ID_HEADER: unsafe_value})
    request_id = response.headers[REQUEST_ID_HEADER]
    assert request_id != unsafe_value
    assert _REQUEST_ID_SHAPE.fullmatch(request_id)


def test_resolve_request_id_only_reuses_safe_tokens() -> None:
    assert resolve_request_id("trace-42") == "trace-42"
    assert resolve_request_id("x" * 129) != "x" * 129
    # Non-ASCII and control characters cannot survive as a token, and an empty header must not
    # be echoed back verbatim.
    assert resolve_request_id("") != ""
    assert resolve_request_id("bad\r\nheader") != "bad\r\nheader"
    assert resolve_request_id("unicode-\u00e4") != "unicode-\u00e4"
    assert _REQUEST_ID_SHAPE.fullmatch(resolve_request_id(None))


def test_route_label_uses_the_matched_route_template() -> None:
    plain = SimpleNamespace(path="/v1/objects/{element_id}", path_format="/v1/objects/{element_id}")
    scope = {"route": plain, "path": "/v1/objects/Machine1", "path_params": {"element_id": "Machine1"}}
    assert route_label(scope) == "/v1/objects/{element_id}"

    # A route included under a prefix reports only its own suffix; the prefix is recovered so the
    # label matches the path a client requested.
    nested = SimpleNamespace(path="/prompts/{name}", path_format="/prompts/{name}")
    nested_scope = {"route": nested, "path": "/mcp/prompts/audit", "path_params": {"name": "audit"}}
    assert route_label(nested_scope) == "/mcp/prompts/{name}"

    assert route_label({}) == UNMATCHED_PATH
    assert route_label({"route": SimpleNamespace(path="")}) == UNMATCHED_PATH
    # Parameters that cannot be filled keep the bare template instead of leaking the concrete URL.
    assert route_label({"route": nested, "path": "/mcp/prompts/audit"}) == "/prompts/{name}"
    mismatched = {"route": nested, "path": "/other/audit", "path_params": {"name": "audit"}}
    assert route_label(mismatched) == "/prompts/{name}"


def test_request_log_line_carries_request_id(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    """The active id is formatted into the text line, not baked into the message.

    The formatter adds it, so a plain-text deployment sees ``request_id=...`` while the JSON
    formatter emits it as a field; the message itself stays identical in both formats.
    """
    with caplog.at_level(logging.INFO, logger="i3x_server.bootstrap.request_middlewares"):
        response = client.get("/health", headers={REQUEST_ID_HEADER: "corr-9"})

    record = next(item for item in caplog.records if "HTTP request method=GET path=/health" in item.getMessage())
    assert response.headers[REQUEST_ID_HEADER] == "corr-9"
    assert _REQUEST_LOG_MESSAGE.fullmatch(record.getMessage())
    with logging_format.request_id_context("corr-9"):
        assert logging_format.TextFormatter().format(record).endswith("request_id=corr-9")


async def test_client_abort_is_logged_and_counted_as_499(caplog: pytest.LogCaptureFixture) -> None:
    """A client disconnect during a response is a 499 abort, not a gateway 500."""
    app = FastAPI()
    request_middlewares.add_request_observability(app)

    @app.get("/stream")
    async def stream() -> None:
        raise RuntimeError("No response returned.")

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "0.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": "/stream",
        "raw_path": b"/stream",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "root_path": "",
        "state": {},
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    with caplog.at_level(logging.INFO, logger="i3x_server.bootstrap.request_middlewares"):
        await app(scope, receive, send)

    start = next(message for message in sent if message["type"] == "http.response.start")
    assert start["status"] == 499
    headers = {key.lower().decode(): value.decode() for key, value in start["headers"]}
    assert _REQUEST_ID_SHAPE.fullmatch(headers["x-request-id"])
    assert any("HTTP request aborted by client" in item.getMessage() for item in caplog.records)
    abort_sample = 'i3x_http_requests_total{method="GET",path="/stream",status="499"}'
    assert _metric_value(_registry_text(), abort_sample) is not None


def test_json_log_format_emits_json_lines_for_requests(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    restored_logging: None,
) -> None:
    monkeypatch.setenv("I3X_LOG_FORMAT", "json")
    monkeypatch.setenv("I3X_ENABLE_MCP", "0")
    monkeypatch.setenv("I3X_SKIP_OPCUA_CONNECT", "1")

    app = create_app()
    with TestClient(app) as client:
        response = client.get("/health", headers={REQUEST_ID_HEADER: "corr-7"})
        assert response.status_code == 200

    # Every line the gateway emitted must be a JSON object, not just the request line.
    payloads = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()]
    startup = next(item for item in payloads if item["message"].startswith("App startup "))
    request_line = next(item for item in payloads if item["message"].startswith("HTTP request "))

    assert request_line["logger"] == "i3x_server.bootstrap.request_middlewares"
    assert request_line["level"] == "INFO"
    assert _REQUEST_LOG_MESSAGE.fullmatch(request_line["message"])
    assert request_line["request_id"] == "corr-7"
    # Startup logs happen outside a request, so they carry no correlation id.
    assert "request_id" not in startup
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", startup["timestamp"])


def test_json_formatter_reports_exception_as_a_field() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging_format.JsonFormatter())
    logger = logging.getLogger("i3x_server.test.json")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        with logging_format.request_id_context("corr-5"):
            try:
                raise ValueError("upstream refused the read")
            except ValueError:
                logger.exception("diagnostics failed")
    finally:
        logger.removeHandler(handler)

    payload = json.loads(stream.getvalue().strip())
    assert payload["level"] == "ERROR"
    # The message stays the human-readable one; the traceback is a separate field, so a log
    # shipper can index it without parsing the message.
    assert payload["message"] == "diagnostics failed"
    assert payload["request_id"] == "corr-5"
    assert payload["exception"].startswith("Traceback")
    assert "upstream refused the read" in payload["exception"]


def test_json_formatter_omits_request_id_outside_a_request() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging_format.JsonFormatter())
    logger = logging.getLogger("i3x_server.test.json.none")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("startup finished")
    finally:
        logger.removeHandler(handler)

    payload = json.loads(stream.getvalue().strip())
    assert payload["message"] == "startup finished"
    assert "request_id" not in payload


def test_configure_logging_installs_the_formatter_on_root(
    capsys: pytest.CaptureFixture[str],
    restored_logging: None,
) -> None:
    """The format knob decides the root handler's formatter and the level.

    ``force=True`` matters here: repeated startups (tests, reload) must not accumulate handlers,
    which would duplicate every log line.
    """
    logging_format.configure_logging("WARNING", "json")
    assert isinstance(logging.root.handlers[0].formatter, logging_format.JsonFormatter)
    assert logging.root.level == logging.WARNING
    assert len(logging.root.handlers) == 1

    logging.getLogger("i3x_server.bootstrap.request_middlewares").info("suppressed below level")
    logging.getLogger("i3x_server.bootstrap.request_middlewares").warning("rate limit tripped")
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["level"] == "WARNING"
    assert payload["message"] == "rate limit tripped"
    assert "suppressed" not in payload["message"]

    logging_format.configure_logging("INFO", "text")
    assert isinstance(logging.root.handlers[0].formatter, logging_format.TextFormatter)
    assert logging.root.level == logging.INFO


def test_log_format_setting_normalizes_and_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("I3X_LOG_FORMAT", "JSON")
    assert Settings().log_format == "json"

    monkeypatch.setenv("I3X_LOG_FORMAT", "structured")
    with pytest.raises(ValidationError):
        Settings()


def test_metrics_endpoint_serves_prometheus_text(client: TestClient) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "# TYPE i3x_http_requests_total counter" in body
    assert "# TYPE i3x_http_request_duration_seconds histogram" in body
    assert "# TYPE i3x_model_nodes gauge" in body


def test_metrics_counts_requests_by_route_template(client: TestClient) -> None:
    sample = 'i3x_http_requests_total{method="GET",path="/mcp/prompts/{name}",status="404"}'
    before = _metric_value(_registry_text(), sample)

    response = client.get("/mcp/prompts/does-not-exist")
    assert response.status_code == 404

    after = _metric_value(_registry_text(), sample)
    assert before is None or after == before + 1
    assert after is not None and after >= 1.0
    body = client.get("/metrics").text
    # The concrete URL never becomes a label, so cardinality stays bounded by the endpoint set.
    assert 'path="/mcp/prompts/does-not-exist"' not in body


def test_metrics_endpoint_is_not_part_of_the_openapi_document(client: TestClient) -> None:
    """Spec-neutral: the scrape route is not an i3X endpoint and never becomes an MCP tool."""
    assert "/metrics" not in client.app.openapi()["paths"]


def test_metrics_gauges_mirror_the_json_diagnostics(client: TestClient) -> None:
    client.get("/health")
    body = client.get("/metrics").text

    assert _metric_value(body, "i3x_model_built") == 1.0
    assert _metric_value(body, "i3x_model_nodes") > 0
    assert _metric_value(body, "i3x_upstream_connected") == 1.0
    assert _metric_value(body, 'i3x_opcua_requests{operation="read"}') == 2451.0
    assert _metric_value(body, 'i3x_opcua_requests{operation="failed"}') == 14.0
    assert _metric_value(body, "i3x_subscriptions_active") == 0.0


def test_metrics_reports_zero_when_a_diagnostics_read_fails(client: TestClient) -> None:
    """A scrape must not fail because the upstream or the subscription runtime is unavailable."""

    class _FailingClient:
        def get_connection_snapshot(self) -> Any:
            raise RuntimeError("connection state unavailable")

        def snapshot_request_metrics(self) -> Any:
            raise RuntimeError("metrics unavailable")

    class _FailingSubscriptionService:
        async def list_subscriptions(
            self,
            client_id: str | None = None,
            subscription_ids: list[str] | None = None,
        ) -> Any:
            raise RuntimeError("inventory unavailable")

    state = gateway_state(client.app)
    # ``Any`` on purpose: these doubles only implement the two read paths the scrape touches.
    failing_client: Any = _FailingClient()
    failing_service: Any = _FailingSubscriptionService()
    state.opcua_client = failing_client
    state.subscription_service = failing_service

    response = client.get("/metrics")
    assert response.status_code == 200
    body = response.text
    assert _metric_value(body, "i3x_upstream_connected") == 0.0
    assert _metric_value(body, 'i3x_opcua_requests{operation="read"}') == 0.0
    assert _metric_value(body, "i3x_subscriptions_active") == 0.0
