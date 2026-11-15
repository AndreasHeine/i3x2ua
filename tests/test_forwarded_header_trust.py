"""Forwarded headers must be trusted only from the known proxy.

uvicorn's ``--proxy-headers`` rewrites ``scope["client"]`` from ``X-Forwarded-For``, and that is the
key the gateway's rate limiter counts on (``http_rate_limit.py``). With ``--forwarded-allow-ips "*"``
every peer is a trusted proxy, so a caller who reaches the app directly can name any client it likes:
each forged address gets its own window, which turns the limiter into a bypass and the audit identity
into something the caller writes. Trust is therefore limited to loopback plus the private network the
proxy lives on, and the documentation keys limiters on the connecting address rather than on a header
the caller controls.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import pytest
from starlette.types import Message, Receive, Scope, Send

from i3x_server.http_rate_limit import RateLimitMiddleware

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
DOCKERFILE = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
RUN_PY = (PROJECT_ROOT / "run.py").read_text(encoding="utf-8")
ENV_EXAMPLE = (PROJECT_ROOT / ".env-example").read_text(encoding="utf-8")
README = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
NGINX_CONF = (PROJECT_ROOT / "nginx" / "nginx.conf").read_text(encoding="utf-8")
LIMITER_SOURCE = (PROJECT_ROOT / "i3x_server" / "http_rate_limit.py").read_text(encoding="utf-8")

# Deployment guidance, not the review notes that recorded the finding.
DOC_FILES = [
    "README.md",
    "docs/NGINX_CONFIGURATION_REFERENCE.md",
    "docs/PRODUCTION_HTTPS_GUIDE.md",
    "docs/QUICK_REFERENCE.md",
    "docs/SCALING_GUIDE.md",
    "nginx/nginx.conf",
    "nginx/entrypoint.sh",
    "docs/examples/multi-instance-path-routing/docker-compose.yml",
    "docs/examples/discovery-subscription-split/docker-compose.yml",
]

TRUST_ENV = "I3X_FORWARDED_ALLOW_IPS"

# Compose files are copyable deployment templates, so each one has to carry the trust list.
COMPOSE_FILES = [
    "docker-compose.yml",
    "docs/examples/multi-instance-path-routing/docker-compose.yml",
    "docs/examples/discovery-subscription-split/docker-compose.yml",
]


def _service_block(compose: str, service: str) -> str:
    """The YAML text of one service, i.e. up to the next service key at indent 2."""
    lines = compose.splitlines()
    start = next(index for index, line in enumerate(lines) if line == f"  {service}:")
    end = next(index for index in range(start + 1, len(lines)) if re.fullmatch(r"  [A-Za-z0-9_-]+:", lines[index]))
    return "\n".join(lines[start:end])


def _yaml_sequence(block: str, key: str) -> list[str]:
    """Items of a YAML block sequence, with quotes and inline comments removed."""
    lines = block.splitlines()
    start = next(index for index, line in enumerate(lines) if line.strip() == f"{key}:")
    items: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue  # comments and blank lines are legal inside a YAML sequence
        if not stripped.startswith("- "):
            break
        items.append(re.sub(r"\s+#.*$", "", stripped[2:]).strip('"').strip("'"))
    return items


def _dockerfile_cmd(dockerfile: str) -> list[str]:
    """The JSON ``CMD`` array the published image starts with."""
    line = next(line for line in dockerfile.splitlines() if line.startswith("CMD "))
    return [str(item) for item in json.loads(line[len("CMD ") :])]


def _flag_value(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


def _trusted_allow_ips(value: str) -> str:
    """The default of an env-backed ``--forwarded-allow-ips`` value."""
    match = re.fullmatch(r"\$\{([A-Za-z0-9_]+):-([^}]+)\}", value)
    assert match is not None, (
        f"--forwarded-allow-ips must be an env-backed list of proxy addresses, not {value!r}: "
        "'*' trusts every peer, so X-Forwarded-For becomes caller-supplied"
    )
    assert match.group(1) == TRUST_ENV, f"the trust list must come from {TRUST_ENV}"
    return match.group(2)


def _parsed_entries(
    default: str,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address | ipaddress.IPv4Network | ipaddress.IPv6Network]:
    entries: list[ipaddress.IPv4Address | ipaddress.IPv6Address | ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in [item.strip() for item in default.split(",") if item.strip()]:
        assert "/" not in raw or raw.count("/") == 1, f"{raw} is not a single network"
        entries.append(ipaddress.ip_network(raw) if "/" in raw else ipaddress.ip_address(raw))
    return entries


def test_compose_trusts_forwarded_headers_only_from_named_addresses() -> None:
    """The shipped deployment must not accept forwarded headers from any peer."""
    command = _yaml_sequence(_service_block(COMPOSE, "i3x2ua"), "command")

    assert "--proxy-headers" in command, "the proxy's reported client address must be used"
    assert _flag_value(command, "--forwarded-allow-ips") is not None

    default = _trusted_allow_ips(_flag_value(command, "--forwarded-allow-ips") or "")
    assert "*" not in default

    entries = _parsed_entries(default)
    assert entries, "the trust list must name at least one address"
    assert any(entry.is_loopback for entry in entries), "a proxy on the same host must stay trusted"
    assert any(isinstance(entry, ipaddress.IPv4Network) and entry.is_private for entry in entries), (
        "the proxy normally lives on the container network, so a private range must be trusted"
    )
    assert all(entry.is_private for entry in entries), (
        "trust must never extend to addresses outside the deployment: a public range would "
        "make the limiter key spoofable by anyone who can connect"
    )


def test_direct_starts_do_not_trust_forwarded_headers_at_all() -> None:
    """An image or `run.py` run without a proxy must rewrite nothing.

    ``--proxy-headers`` with the default trust list would still trust loopback, and a container
    started directly has no proxy in front: the header would come from whoever connected.
    """
    assert "--proxy-headers" not in _dockerfile_cmd(DOCKERFILE)
    assert "--proxy-headers" not in RUN_PY
    assert "--forwarded-allow-ips" not in RUN_PY


@pytest.mark.parametrize("path", DOC_FILES)
def test_no_limiter_zone_is_keyed_on_a_caller_header(path: str) -> None:
    """A zone keyed on `$http_x_forwarded_for` hands the bucket to whoever sends the header.

    Prose may name the header to warn against it; what must not appear is a directive keyed on it.
    """
    text = (PROJECT_ROOT / path).read_text(encoding="utf-8")

    assert re.search(r"limit_(?:req|conn)_zone\s+\$http_x_forwarded_for", text) is None, (
        f"{path} must key limiters on the connecting address"
    )


@pytest.mark.parametrize("path", DOC_FILES)
def test_docs_that_discuss_forwarded_trust_name_the_trusted_addresses(path: str) -> None:
    """Reader-facing guidance must say which addresses may rewrite the client identity."""
    text = (PROJECT_ROOT / path).read_text(encoding="utf-8")

    if "--forwarded-allow-ips" not in text and "X-Forwarded-For" not in text:
        return

    assert TRUST_ENV in text, f"{path} discusses forwarded headers without naming {TRUST_ENV}"


def test_shipped_proxy_key_is_the_remote_address() -> None:
    """Regression: nginx's shipped zones already key on the connecting address."""
    assert "limit_req_zone $binary_remote_addr" in NGINX_CONF
    assert "limit_conn_zone $binary_remote_addr" in NGINX_CONF


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_compose_templates_trust_named_addresses(path: str) -> None:
    """Every copyable compose template must restrict forwarded trust, not open it."""
    text = (PROJECT_ROOT / path).read_text(encoding="utf-8")

    assert "--forwarded-allow-ips" in text, f"{path} must name the trusted proxies explicitly"
    assert re.search(r"--forwarded-allow-ips\s*\n\s*-?\s*\"\*\"", text) is None, f"{path} trusts every peer"
    assert f"${{{TRUST_ENV}:-" in text, f"{path} must read the trust list from {TRUST_ENV}"


def test_env_template_documents_the_trust_list_and_the_limiter_key() -> None:
    """Operators need to know which addresses rewrite the client identity."""
    assert TRUST_ENV in ENV_EXAMPLE
    assert "X-Forwarded-For" in ENV_EXAMPLE
    assert "X-Forwarded-For" in README


def test_limiter_source_documents_the_trusted_key() -> None:
    """The limiter docstring must not describe a spoofable key as the connecting IP."""
    docstring = LIMITER_SOURCE.split('"""')[1]

    assert TRUST_ENV in docstring
    assert "X-Forwarded-For" in docstring


async def _receive() -> Message:
    """A minimal ASGI receive: the middleware under test never reads the body."""
    return {"type": "http.request"}


class _StatusRecorder:
    """Collects the response status of each call so assertions can inspect every request."""

    def __init__(self) -> None:
        self.statuses: list[int] = []

    async def __call__(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.statuses.append(int(message["status"]))


def _http_scope(client: tuple[str, int], forwarded_for: str) -> Scope:
    """An HTTP scope with the connecting address and one `X-Forwarded-For` header."""
    headers = [(b"x-forwarded-for", forwarded_for.encode("latin-1"))]
    return cast(Scope, {"type": "http", "client": client, "headers": headers})


def _compose_trust_default() -> str:
    """The trust list the shipped command passes to uvicorn."""
    command = _yaml_sequence(_service_block(COMPOSE, "i3x2ua"), "command")
    return _trusted_allow_ips(_flag_value(command, "--forwarded-allow-ips") or "")


# uvicorn types its middleware against its own scope/message shapes; the gateway and its tests use
# Starlette's, which carry the same values at runtime. The constructor argument is typed against
# uvicorn's event types, so it is passed as Any and the resulting middleware is cast back to the
# Starlette-shaped callable the gateway's own middleware uses.
_AsgiApp = Callable[[Scope, Receive, Send], Awaitable[None]]


async def _call_middleware(trusted: str, client: tuple[str, int], forwarded_for: str) -> tuple[str, int]:
    """Run uvicorn's proxy-header middleware exactly as the shipped command line configures it."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    seen: list[tuple[str, int]] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        client_value = scope.get("client")
        assert client_value is not None
        seen.append((client_value[0], client_value[1]))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    middleware = cast(_AsgiApp, ProxyHeadersMiddleware(cast(Any, app), trusted_hosts=trusted))
    await middleware(_http_scope(client, forwarded_for), _receive, _noop_send)

    assert seen
    return seen[0]


async def _noop_send(message: Message) -> None:
    return None


async def test_a_direct_caller_cannot_name_a_client() -> None:
    """A peer outside the trust list keeps its own address; the forged header is ignored."""
    client = await _call_middleware(_compose_trust_default(), ("203.0.113.7", 51000), "198.51.100.10, 203.0.113.7")

    assert client == ("203.0.113.7", 51000)


async def test_the_proxy_reports_the_real_client() -> None:
    """nginx appends its own address, so the first untrusted address is the client."""
    client = await _call_middleware(_compose_trust_default(), ("172.18.0.3", 50000), "203.0.113.7, 172.18.0.3")

    assert client == ("203.0.113.7", 0)


async def test_forged_identities_do_not_buy_extra_windows() -> None:
    """One peer hammering the limiter with a different forged address each time stays limited.

    The rate limiter counts ``scope["client"]``, and uvicorn's proxy-header middleware runs
    outermost, so with ``--forwarded-allow-ips "*"`` the key becomes the header the caller writes
    and every forged identity gets its own window. Composing the two in that order is what proves
    the trust list closes the bypass.
    """
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    limiter = RateLimitMiddleware(app, limit_per_minute=2)
    handler = cast(_AsgiApp, ProxyHeadersMiddleware(cast(Any, limiter), trusted_hosts=_compose_trust_default()))

    recorder = _StatusRecorder()
    for forged in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
        await handler(_http_scope(("203.0.113.7", 40000), forged), _receive, recorder)

    assert recorder.statuses == [200, 200, 429], "the untrusted peer must share one window, not one per header"
