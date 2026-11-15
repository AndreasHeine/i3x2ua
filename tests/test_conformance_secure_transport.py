"""The conformance gate must be able to run against the secured deployment stack.

The fixture-server check runs against plain HTTP on the gateway's own port. A deployment runs behind
nginx: TLS on 8443, Basic Auth on everything except the public ``/v1/info``, and HTTP redirected to
HTTPS. Those layers are the parts a plain-HTTP run cannot exercise, so the runner has to reach the
gateway through them: a scheme, a public host/port, credentials for the suite, and a TLS opt-out for
the self-signed development certificate.
"""

from __future__ import annotations

import base64
import http.server
import json
import ssl
import threading
from pathlib import Path
from typing import Any

import pytest

import scripts.run_conformance_headless as headless
from scripts.generate_https_dev_cert import generate_certificate


def _endpoint_args(**overrides: Any) -> Any:
    defaults = ["--scheme", "https", "--public-host", "127.0.0.1", "--public-port", "8443"]
    return headless._build_parser().parse_args(defaults + [str(value) for value in overrides.values()])


def _serve_https(tmp_path: Path) -> tuple[str, threading.Thread, http.server.ThreadingHTTPServer]:
    """Serve a self-signed HTTPS endpoint that answers ``/v1/info`` with 200."""
    cert_path, key_path, _ = generate_certificate(
        out_dir=tmp_path / "certs",
        common_name="localhost",
        organization="i3x2ua",
        country="DE",
        days_valid=1,
        dns_names=[],
        ip_addresses=["127.0.0.1"],
        include_hostname=False,
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/v1/info":
                self.send_response(404)
                self.end_headers()
                return
            payload = json.dumps({"name": "i3x2ua"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            del format, args

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.socket.getsockname()[:2]
    return f"https://{host}:{port}/v1/info", thread, server


def test_https_endpoint_is_built_from_the_public_host_and_port() -> None:
    """The suite must be pointed at the proxy, not at the gateway port behind it."""
    assert headless._endpoint("https", "127.0.0.1", 8443) == "https://127.0.0.1:8443/v1"
    assert headless._endpoint("http", "127.0.0.1", 8000) == "http://127.0.0.1:8000/v1"


def test_the_parser_accepts_the_secured_stack_options() -> None:
    """CI invokes these flags; a typo in a flag name silently keeps the plain-HTTP default."""
    args = headless._build_parser().parse_args(
        [
            "--scheme",
            "https",
            "--public-host",
            "127.0.0.1",
            "--public-port",
            "8443",
            "--basic",
            "ci:secret",
            "--insecure",
        ]
    )

    assert args.scheme == "https"
    assert args.public_host == "127.0.0.1"
    assert args.public_port == 8443
    assert args.basic == "ci:secret"
    assert args.insecure is True


def test_the_parser_defaults_to_the_plain_gateway_endpoint() -> None:
    """The existing fixture-server check must keep running unchanged."""
    args = headless._build_parser().parse_args([])

    assert args.scheme == "http"
    assert args.public_host == args.i3x_host
    assert args.public_port == args.i3x_port
    assert args.basic is None
    assert args.insecure is False


def test_the_suite_command_forwards_credentials_and_the_tls_opt_out() -> None:
    """nginx answers 401 without credentials and rejects the self-signed cert without the opt-out."""
    command = headless._suite_command("node", Path("i3x-test"), "https://127.0.0.1:8443/v1", None, False, None)
    assert command[:3] == ["node", "i3x-test", "run"]
    assert "--basic" not in command
    assert "--insecure" not in command

    secured = headless._suite_command("node", Path("i3x-test"), "https://127.0.0.1:8443/v1", "ci:secret", True, None)
    assert secured[secured.index("--basic") + 1] == "ci:secret"
    assert "--insecure" in secured


def test_basic_credentials_must_be_user_colon_password() -> None:
    """A malformed credential would send an empty or wrong Authorization header and still look set."""
    assert headless._auth_header(None) == {}
    encoded = base64.b64encode(b"ci-user:s e c r e t").decode("ascii")
    assert headless._auth_header("ci-user:s e c r e t") == {"Authorization": f"Basic {encoded}"}

    for malformed in ("", ":password", "user-only"):
        with pytest.raises(ValueError):
            headless._auth_header(malformed)


def test_insecure_mode_verifies_nothing_and_the_default_still_verifies() -> None:
    """Self-signed development certificates are what the CI stack presents."""
    context = headless._ssl_context(insecure=True)
    assert context is not None
    assert context.verify_mode == ssl.CERT_NONE
    assert context.check_hostname is False
    assert headless._ssl_context(insecure=False) is None


def test_readiness_probe_crosses_tls_and_authentication(tmp_path: Path) -> None:
    """A model that is ready behind the proxy is only visible through TLS and credentials."""
    url, thread, server = _serve_https(tmp_path)
    try:
        assert headless._wait_for_http(url, 3.0, insecure=True, basic="ci:secret") is True
        # Regression: the same endpoint must not pass without the opt-out, so a run that drops
        # --insecure cannot silently claim a secured stack was verified.
        assert headless._wait_for_http(url, 1.0) is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
