"""The shipped deployment must not carry credentials that work out of the box.

The gateway has no application-level auth: the reverse proxy is the security boundary, and
compose enables it (``NGINX_BASIC_AUTH_ENABLED: 1``) together with ``I3X_ENABLE_WRITES: 1`` and
``I3X_ENABLE_MCP: 1``. Literal credentials in the committed file therefore hand anyone who
checks out the repository write access to industrial values, and the pairing is exactly what an
attacker reads first. The credentials come from the operator's ``.env`` instead, so nginx's
existing fail-fast path refuses to start until they are set (``nginx/entrypoint.sh``).
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
ENTRYPOINT = (PROJECT_ROOT / "nginx" / "entrypoint.sh").read_text(encoding="utf-8")
ENV_EXAMPLE = (PROJECT_ROOT / ".env-example").read_text(encoding="utf-8")

# The credentials the repository used to ship. Anything carrying one of them is a working
# default, not a placeholder.
SHIPPED_USER = "admin"
SHIPPED_PASSWORD = "pw1"
TEXT_FILES = [
    "docker-compose.yml",
    ".env-example",
    "docs/.env.production.example",
    "README.md",
    "docs/QUICK_REFERENCE.md",
    "docs/PRODUCTION_HTTPS_GUIDE.md",
    "docs/examples/discovery-subscription-split/docker-compose.yml",
    "docs/examples/multi-instance-path-routing/docker-compose.yml",
    "samples/README.md",
    "samples/client.py",
    "samples/subscription_client.py",
    "nginx/entrypoint.sh",
]


def _service_block(compose: str, service: str) -> str:
    """The YAML text of one service, i.e. up to the next service key at indent 2."""
    lines = compose.splitlines()
    start = next(index for index, line in enumerate(lines) if line == f"  {service}:")
    end = next(index for index in range(start + 1, len(lines)) if re.fullmatch(r"  [A-Za-z0-9_-]+:", lines[index]))
    return "\n".join(lines[start:end])


def _env_values(block: str) -> dict[str, str]:
    """Mapping-style ``KEY: value`` entries of a service's ``environment:`` block."""
    lines = block.splitlines()
    start = next(index for index, line in enumerate(lines) if line.strip() == "environment:")
    values: dict[str, str] = {}
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line.startswith("      "):
            break
        match = re.fullmatch(r"([A-Za-z0-9_]+):\s*(.*)", stripped)
        if match is None:
            break
        value = re.sub(r"\s+#.*$", "", match.group(2))
        values[match.group(1)] = value.strip('"').strip("'")
    return values


def _is_truthy(value: str) -> bool:
    return value.strip().strip('"').strip("'").lower() in {"1", "true", "yes", "on"}


def _locations(location: str) -> list[str]:
    """The bodies of every generated nginx location with this path (HTTP and HTTPS blocks)."""
    lines = ENTRYPOINT.splitlines()
    blocks: list[str] = []
    for index, line in enumerate(lines):
        if line.strip() != f"location = {location} {{":
            continue
        end = next(end for end in range(index + 1, len(lines)) if lines[end].strip() == "}")
        blocks.append("\n".join(lines[index:end]))
    return blocks


def test_compose_ships_no_working_basic_auth_credentials() -> None:
    """The credentials must be read from the operator's environment, not committed."""
    env = _env_values(_service_block(COMPOSE, "nginx"))

    assert env["NGINX_BASIC_AUTH_USER"] == "${NGINX_BASIC_AUTH_USER:-}"
    assert env["NGINX_BASIC_AUTH_PASSWORD"] == "${NGINX_BASIC_AUTH_PASSWORD:-}"


def test_writes_and_mcp_are_never_shipped_behind_an_open_proxy() -> None:
    """The gateway authenticates nothing itself, so auth stays enabled where writes are."""
    gateway_env = _env_values(_service_block(COMPOSE, "i3x2ua"))
    nginx_env = _env_values(_service_block(COMPOSE, "nginx"))

    assert _is_truthy(gateway_env["I3X_ENABLE_WRITES"])
    assert _is_truthy(gateway_env["I3X_ENABLE_MCP"])
    assert _is_truthy(nginx_env["NGINX_BASIC_AUTH_ENABLED"])


def test_compose_ships_neither_the_shipped_user_nor_the_shipped_password() -> None:
    """Both halves of the credential pair are placeholders, so nothing works out of the box."""
    env = _env_values(_service_block(COMPOSE, "nginx"))

    assert SHIPPED_USER not in env["NGINX_BASIC_AUTH_USER"]
    assert SHIPPED_PASSWORD not in env["NGINX_BASIC_AUTH_PASSWORD"]


def test_missing_credentials_refuse_to_start_rather_than_open_the_proxy() -> None:
    """With auth enabled and no credentials the proxy must fail, not serve unauthenticated."""
    auth_section = ENTRYPOINT.split('if is_truthy "$basic_auth_enabled"; then')[1].split("fi\n")[0]

    assert "NGINX_BASIC_AUTH_USER" in auth_section
    assert "NGINX_BASIC_AUTH_PASSWORD" in auth_section
    assert "exit 1" in auth_section


def test_unauthenticated_info_route_stays_unauthenticated() -> None:
    """Regression: the nginx health probe reads /v1/info without credentials.

    Removing the shipped credentials must not push the probe behind Basic Auth, or every
    container would report unhealthy and the gateway would never start (compose waits on
    ``service_healthy``).
    """
    assert _locations("/v1/info"), "the info location must be generated for both entry points"
    for block in _locations("/v1/info"):
        assert "auth_block" not in block


def test_no_tracked_file_quotes_the_shipped_credentials() -> None:
    """Docs and samples must point at the operator's credentials, not a working password."""
    offenders = [path for path in TEXT_FILES if SHIPPED_PASSWORD in (PROJECT_ROOT / path).read_text(encoding="utf-8")]

    assert offenders == [], f"{SHIPPED_PASSWORD} must not appear in tracked files: {offenders}"


def test_env_template_leaves_credentials_empty() -> None:
    """The template documents the keys without shipping a value they could fall back to."""
    assert re.search(r"^NGINX_BASIC_AUTH_USER=$", ENV_EXAMPLE, re.MULTILINE)
    assert re.search(r"^NGINX_BASIC_AUTH_PASSWORD=$", ENV_EXAMPLE, re.MULTILINE)
