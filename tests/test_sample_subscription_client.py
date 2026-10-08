from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

import pytest

from samples import subscription_client
from samples.client import I3XRequestError

UPDATE = {
    "sequenceNumber": 7,
    "elementId": "nsu=http://example.com/;s=Temperature",
    "value": {"reading": 21.5},
    "quality": "Good",
    "timestamp": "2026-10-08T14:00:00Z",
}


@dataclass
class ServerState:
    requests: list[tuple[str, dict[str, Any], str | None]] = field(default_factory=list)
    fail_registration: bool = False
    fail_delete: bool = False
    stream_status: int = 200
    stream_content_type: str = "text/event-stream"
    stream_body: bytes = (
        f": connected\r\n\r\n: keepalive\r\n\r\ndata: {json.dumps([UPDATE])}\r\n\r\nevent: close\r\ndata: {{}}\r\n\r\n"
    ).encode()
    sync_calls: int = 0


@pytest.fixture
def subscription_server() -> Iterator[tuple[str, ServerState]]:
    state = ServerState()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.requests.append((self.path, body, self.headers.get("Authorization")))
            status = 200
            content_type = "application/json"
            payload: dict[str, Any]
            if self.path == "/v1/subscriptions":
                payload = {"success": True, "result": {"subscriptionId": "sub-1"}}
            elif self.path == "/v1/subscriptions/register":
                payload = {
                    "success": not state.fail_registration,
                    "results": [
                        {
                            "success": not state.fail_registration,
                            "elementId": element_id,
                            "error": {"code": 404, "message": "Unknown element"} if state.fail_registration else None,
                        }
                        for element_id in body["elementIds"]
                    ],
                }
            elif self.path == "/v1/subscriptions/delete":
                payload = {
                    "success": not state.fail_delete,
                    "results": [{"success": not state.fail_delete, "subscriptionId": "sub-1"}],
                }
            elif self.path == "/v1/subscriptions/sync":
                state.sync_calls += 1
                payload = {
                    "success": True,
                    "result": [{"sequenceNumber": 7, "updates": [UPDATE]}] if state.sync_calls == 1 else [],
                }
                if state.sync_calls == 1:
                    status = 206
                    payload["responseDetail"] = {"status": 206, "detail": "Updates dropped due to queue overflow"}
            elif self.path == "/v1/subscriptions/stream":
                status = state.stream_status
                content_type = state.stream_content_type
                payload = {}
            else:
                status = 404
                payload = {"error": "Unknown endpoint"}
            data = state.stream_body if self.path.endswith("/stream") else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _arguments(monkeypatch: pytest.MonkeyPatch, base_url: str, *options: str) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "subscription_client",
            "--base-url",
            base_url,
            "--client-id",
            "test-client",
            "--element-ids",
            "ns=2;i=1",
            "ns=2;i=2",
            *options,
        ],
    )


def test_sse_lifecycle_and_auth(
    subscription_server: tuple[str, ServerState], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base_url, state = subscription_server
    _arguments(monkeypatch, base_url, "--mode", "sse", "--username", "admin", "--password", "pw1", "--max-depth", "1")

    assert subscription_client.main() == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out) == UPDATE
    assert "Stream closed by server" in captured.err
    assert "Deleted subscription" in captured.err
    assert [path for path, _, _ in state.requests] == [
        "/v1/subscriptions",
        "/v1/subscriptions/register",
        "/v1/subscriptions/stream",
        "/v1/subscriptions/delete",
    ]
    assert all(auth == "Basic YWRtaW46cHcx" for _, _, auth in state.requests)
    assert state.requests[1][1] == {
        "clientId": "test-client",
        "subscriptionId": "sub-1",
        "elementIds": ["ns=2;i=1", "ns=2;i=2"],
        "maxDepth": 1,
    }
    assert state.requests[2][1] == {"clientId": "test-client", "subscriptionId": "sub-1"}
    assert state.requests[-1][1] == {"clientId": "test-client", "subscriptionIds": ["sub-1"]}


def test_poll_acknowledges_printed_updates_and_preserves_ack_on_empty_sync(
    subscription_server: tuple[str, ServerState], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base_url, state = subscription_server
    _arguments(monkeypatch, base_url, "--poll-interval", "0.25")
    intervals: list[float] = []

    def stop_after_three_syncs(interval: float) -> None:
        intervals.append(interval)
        if len(intervals) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", stop_after_three_syncs)

    assert subscription_client.main() == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out) == UPDATE
    assert "queue overflow" in captured.err
    assert "Stopping subscription" in captured.err
    assert intervals == [0.25, 0.25, 0.25]
    sync_bodies = [body for path, body, _ in state.requests if path.endswith("/sync")]
    assert "acknowledgeSequence" not in sync_bodies[0]
    assert [body["acknowledgeSequence"] for body in sync_bodies[1:]] == [7, 7]
    assert state.requests[1][1]["maxDepth"] is None
    assert state.requests[-1][0] == "/v1/subscriptions/delete"


def test_registration_failure_is_reported_and_subscription_deleted(
    subscription_server: tuple[str, ServerState], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base_url, state = subscription_server
    state.fail_registration = True
    _arguments(monkeypatch, base_url, "--mode", "sse")

    assert subscription_client.main() == 1

    assert "Unknown element" in capsys.readouterr().err
    assert [path for path, _, _ in state.requests] == [
        "/v1/subscriptions",
        "/v1/subscriptions/register",
        "/v1/subscriptions/delete",
    ]


@pytest.mark.parametrize(
    ("body", "status", "content_type", "error"),
    [
        (b"", 200, "text/event-stream", "ended without"),
        (b"data: not-json\n\n", 200, "text/event-stream", "Subscription failed"),
        (b"unauthorized", 401, "application/json", "HTTP 401: unauthorized"),
        (b"{}", 200, "application/json", "expected text/event-stream"),
    ],
)
def test_stream_errors_are_reported_and_subscription_deleted(
    subscription_server: tuple[str, ServerState],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    body: bytes,
    status: int,
    content_type: str,
    error: str,
) -> None:
    base_url, state = subscription_server
    state.stream_body, state.stream_status, state.stream_content_type = body, status, content_type
    _arguments(monkeypatch, base_url, "--mode", "sse")

    assert subscription_client.main() == 1

    assert error in capsys.readouterr().err
    assert state.requests[-1][0] == "/v1/subscriptions/delete"


def test_cleanup_failure_has_nonzero_exit_code(
    subscription_server: tuple[str, ServerState], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base_url, state = subscription_server
    state.fail_delete = True
    _arguments(monkeypatch, base_url, "--mode", "sse")

    assert subscription_client.main() == 1
    assert "cleanup failed" in capsys.readouterr().err


def test_interrupt_during_sse_deletes_subscription(
    subscription_server: tuple[str, ServerState], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base_url, state = subscription_server
    _arguments(monkeypatch, base_url, "--mode", "sse")

    def interrupted_events(lines: Any) -> Iterator[tuple[str, str]]:
        yield "message", json.dumps([UPDATE])
        raise KeyboardInterrupt

    monkeypatch.setattr(subscription_client, "_sse_events", interrupted_events)

    assert subscription_client.main() == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out) == UPDATE
    assert "Stopping subscription" in captured.err
    assert state.requests[-1][0] == "/v1/subscriptions/delete"


def test_sse_parser_handles_comments_multiline_data_and_multiple_events() -> None:
    lines = [
        b": connected\r\n",
        b"\r\n",
        b"id: 1\n",
        b"event: message\n",
        b"data: [\n",
        b'data: {"sequenceNumber": 1}]\n',
        b"\n",
        b": keepalive\n",
        b"\n",
        b"data: []\n",
        b"\n",
        b"event: close\n",
        b"data: {}\n",
        b"\n",
    ]
    assert list(subscription_client._sse_events(lines)) == [
        ("message", '[\n{"sequenceNumber": 1}]'),
        ("message", "[]"),
        ("close", "{}"),
    ]


@pytest.mark.parametrize("updates", [None, {}, [None], [{"sequenceNumber": 1}], [{**UPDATE, "sequenceNumber": True}]])
def test_invalid_notifications_are_not_printed(updates: Any, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(I3XRequestError):
        subscription_client.I3XSubscriptionClient.print_updates(updates)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "options",
    [
        ("--poll-interval", "0"),
        ("--poll-interval", "nan"),
        ("--timeout", "-1"),
        ("--timeout", "inf"),
        ("--max-depth", "-1"),
        ("--element-ids", ""),
    ],
)
def test_invalid_cli_input_is_rejected(monkeypatch: pytest.MonkeyPatch, options: tuple[str, ...]) -> None:
    _arguments(monkeypatch, "http://127.0.0.1:1", *options)
    with pytest.raises(SystemExit) as exc_info:
        subscription_client.main()
    assert exc_info.value.code == 2
