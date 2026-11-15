"""The proxy log is the audit trail, so it must carry the correlation id.

The compose deployment suppresses the gateway's per-request INFO line (``I3X_LOG_LEVEL: WARN``)
and uvicorn access logs (``--no-access-log``), which makes the proxy the only record of what was
asked for. Without a request id in that record an incident cannot be traced across the edge, so
nginx derives one (``$request_id``), reuses a client-supplied id only when it matches the
gateway's safe token pattern, logs it, and forwards it as ``X-Request-ID`` so any gateway log
line about the same request carries the same id.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from i3x_server.bootstrap.request_middlewares import REQUEST_ID_HEADER, resolve_request_id

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NGINX_CONF = (PROJECT_ROOT / "nginx" / "nginx.conf").read_text(encoding="utf-8")
ENTRYPOINT = (PROJECT_ROOT / "nginx" / "entrypoint.sh").read_text(encoding="utf-8")

# The gateway only reuses ids matching this pattern (``request_middlewares.py``).
SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
# nginx's own ``$request_id`` is a 32-character hex string.
NGINX_GENERATED_ID = "9f2c4a1b6e0d3f8a5c7b1e2d4a6f8b0c"


def _heredocs(script: str) -> dict[str, str]:
    """The heredoc bodies of the shell script, keyed by the variable they assign."""
    blocks: dict[str, str] = {}
    for match in re.finditer(r"(\w+)=\$\(cat <<EOF\r?\n(.*?)\r?\nEOF\r?\n?\)", script, re.DOTALL):
        blocks[match.group(1)] = match.group(2)
    return blocks


def test_nginx_config_derives_a_correlation_id() -> None:
    """A safe incoming id is reused; anything else falls back to nginx's own id."""
    assert "map $http_x_request_id $i3x_request_id" in NGINX_CONF
    assert '"~^[A-Za-z0-9._-]{1,128}$"' in NGINX_CONF
    assert "$request_id" in NGINX_CONF


def test_nginx_log_format_logs_the_correlation_id() -> None:
    """Every proxy log line names the request it belongs to."""
    log_format = re.search(r"log_format upstream_timing\r?\n(.*?);", NGINX_CONF, re.DOTALL)
    assert log_format is not None
    assert "rid=$i3x_request_id" in log_format.group(1)


@pytest.mark.parametrize("block", ["proxy_block", "sse_proxy_block", "mcp_proxy_block"])
def test_proxy_blocks_forward_the_correlation_id(block: str) -> None:
    """The gateway must see the same id the proxy logged, including streams and MCP."""
    body = _heredocs(ENTRYPOINT)[block]

    # Heredocs escape nginx variables (``\$host``) so the shell never expands them.
    assert "proxy_set_header X-Request-ID \\$i3x_request_id;" in body


@pytest.mark.parametrize("block", ["proxy_block", "sse_proxy_block", "mcp_proxy_block"])
def test_proxy_blocks_escape_nginx_variables(block: str) -> None:
    """The heredocs are unquoted, so a raw ``$`` would be shell-expanded.

    ``set -u`` then aborts the entrypoint on an unset name, so nginx variables (even in
    comments) must stay escaped as ``\\$``.
    """
    body = _heredocs(ENTRYPOINT)[block]

    assert not re.search(r"(?<!\\)\$", body)


def test_gateway_reuses_an_nginx_generated_id() -> None:
    """Regression: nginx's own id shape must survive the gateway's validation.

    Otherwise every proxied request gets a freshly generated id at the gateway and the two log
    records stop agreeing.
    """
    assert SAFE_REQUEST_ID.fullmatch(NGINX_GENERATED_ID)
    assert resolve_request_id(NGINX_GENERATED_ID) == NGINX_GENERATED_ID


def test_gateway_still_rejects_unsafe_incoming_ids() -> None:
    """Edge validation is defence in depth, not a replacement for the gateway's own."""
    forged = "line1\nline2"
    resolved = resolve_request_id(forged)

    assert resolved != forged
    assert SAFE_REQUEST_ID.fullmatch(resolved)


def test_client_supplied_id_is_echoed_on_the_response(client: TestClient) -> None:
    """The correlation id the proxy logged is the id the caller can read back."""
    response = client.get("/health", headers={REQUEST_ID_HEADER: NGINX_GENERATED_ID})

    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER] == NGINX_GENERATED_ID
