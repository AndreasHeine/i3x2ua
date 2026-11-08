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


_ROOT_OBJECT: dict[str, Any] = {
    "elementId": "asset-root",
    "displayName": "Plant",
    "typeElementId": "type-asset",
    "isComposition": False,
    "metadata": {"relationships": {"HasChildren": ["machine-01", "gone-branch"], "HasComponent": ["machine-01"]}},
}

_CHILD_OBJECT: dict[str, Any] = {
    "elementId": "machine-01",
    "displayName": "Mixer-01",
    "typeElementId": "type-machine",
    "isComposition": True,
    # A component that references its own parent: discovery deduplicates it, but a
    # recursive printer must not walk back into the root forever.
    "metadata": {"relationships": {"HasChildren": ["asset-root"]}},
}

_OBJECT_TYPES: dict[str, dict[str, Any]] = {
    "type-asset": {"displayName": "Asset", "schema": {"properties": {}}},
    "type-machine": {"displayName": "Machine", "schema": {"properties": {"Temperature": {"type": "number"}}}},
}


@pytest.fixture
def discovery_server() -> Iterator[str]:
    """Minimal bulk-capable i3X server for the discovery pattern.

    A bulk call answers HTTP 200 while individual items fail, which is how the real
    gateway reports an unknown elementId or an upstream failure.
    """

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _respond(self, payload: dict[str, Any], status: int = 200) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path.startswith("/v1/info"):
                self._respond({"success": True, "result": {"serverName": "fixture", "specVersion": "1.0"}})
            elif self.path.startswith("/v1/objects"):
                self._respond({"success": True, "result": [_ROOT_OBJECT]})
            else:
                self._respond({"success": False}, status=404)

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            element_ids: list[str] = json.loads(self.rfile.read(length).decode("utf-8")).get("elementIds", [])

            if self.path.startswith("/v1/objects/list"):
                results = [
                    {"elementId": element_id, "success": True, "result": _CHILD_OBJECT}
                    if element_id != "gone-branch"
                    else {"elementId": element_id, "success": False, "errorClass": "notFound"}
                    for element_id in element_ids
                ]
                self._respond({"success": True, "results": results})
            elif self.path.startswith("/v1/objecttypes/query"):
                results = [
                    {"elementId": element_id, "success": True, "result": _OBJECT_TYPES[element_id]}
                    if element_id in _OBJECT_TYPES
                    else {"elementId": element_id, "success": False, "errorClass": "notFound"}
                    for element_id in element_ids
                ]
                self._respond({"success": True, "results": results})
            else:
                self._respond({"success": False}, status=404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_discovery_reports_failed_bulk_items(discovery_server: str, capsys: pytest.CaptureFixture[str]) -> None:
    client = I3XClient(discovery_server)
    client.discover(max_nodes=10, max_depth=2)

    captured = capsys.readouterr()
    assert "failed item" in captured.err
    assert "gone-branch" in captured.err

    # The failed branch must not appear in the tree, and the reachable child must.
    assert "gone-branch" not in client.objects_by_id
    assert client.objects_by_id["machine-01"].parent_id == "asset-root"


def test_print_tree_terminates_and_deduplicates_cyclic_relationships(
    discovery_server: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = I3XClient(discovery_server)
    client.discover(max_nodes=10, max_depth=2)
    capsys.readouterr()

    client.print_tree()

    out = capsys.readouterr().out
    assert out.count("- Plant") == 1
    assert out.count("- Mixer-01") == 1
