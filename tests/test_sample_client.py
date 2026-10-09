from __future__ import annotations

import json
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

import pytest

from samples.client import I3XClient, I3XRequestError


@pytest.fixture
def keepalive_server() -> Iterator[tuple[str, list[int], dict[str, bool]]]:
    client_ports: list[int] = []
    options = {"close_next": False}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            client_ports.append(self.client_address[1])
            status = 404 if self.path.endswith("/missing") else 200
            data = json.dumps({"success": status == 200, "path": self.path}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if options["close_next"]:
                options["close_next"] = False
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", client_ports, options
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_requests_reuse_one_keepalive_connection(keepalive_server: tuple[str, list[int], dict[str, bool]]) -> None:
    base_url, client_ports, _ = keepalive_server
    client = I3XClient(base_url)

    for _ in range(5):
        assert client.get_info()["path"] == "/v1/info"

    assert len(client_ports) == 5
    assert len(set(client_ports)) == 1


def test_request_reconnects_after_server_closes_connection(
    keepalive_server: tuple[str, list[int], dict[str, bool]],
) -> None:
    base_url, client_ports, options = keepalive_server
    client = I3XClient(base_url)

    options["close_next"] = True
    client.get_info()
    client.get_info()

    assert len(set(client_ports)) == 2


def test_http_error_and_connection_error_raise_request_error(
    keepalive_server: tuple[str, list[int], dict[str, bool]],
) -> None:
    base_url, _, _ = keepalive_server

    with pytest.raises(I3XRequestError, match=r"GET /missing -> HTTP 404"):
        I3XClient(base_url)._get("/missing")
    with pytest.raises(I3XRequestError, match=r"GET /info -> "):
        I3XClient("http://127.0.0.1:1/v1", timeout=2).get_info()
