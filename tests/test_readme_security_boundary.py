"""The proxy-less quickstarts must be labelled development-only.

The gateway enforces no HTTP authentication scheme: TLS termination and authentication live in
the reverse proxy, so ``uv run uvicorn ...`` and ``docker run -p 8080:8000`` expose the API -
writes included, once ``I3X_ENABLE_WRITES=1`` - in plaintext to whatever network the port is
reachable on. The README previously presented both without that caveat, so a reader following it
on a machine on the plant network publishes industrial values. The production boundary and its
TLS/auth prerequisites are written down next to those commands instead of only in the OpenAPI
section.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
README = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
SECURITY = (PROJECT_ROOT / "SECURITY.md").read_text(encoding="utf-8")

DEV_ONLY_MARKER = "development only"


def _section(markdown: str, heading: str) -> str:
    """The text of one ``##`` section, up to the next ``##`` heading."""
    match = re.search(rf"^## {re.escape(heading)}$", markdown, re.MULTILINE)
    assert match is not None, f"README must have a '## {heading}' section"
    rest = markdown[match.end() :]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def test_proxy_less_quickstart_is_development_only() -> None:
    """The uvicorn quickstart binds a plaintext port the gateway does not authenticate."""
    quick_start = _section(README, "Quick Start")

    assert DEV_ONLY_MARKER in quick_start.lower()
    assert "authentication" in quick_start.lower()
    assert "plaintext" in quick_start.lower() or "TLS" in quick_start


def test_quickstart_warns_before_the_first_command() -> None:
    """A reader copying commands must see the warning before running anything."""
    quick_start = _section(README, "Quick Start")
    warning = quick_start.lower().find(DEV_ONLY_MARKER)
    command = quick_start.find("uvicorn")

    assert warning != -1
    assert command != -1
    assert warning < command


def _code_blocks(section: str) -> list[str]:
    """The contents of the fenced code blocks in a section."""
    return re.findall(r"^```[a-z]*\n(.*?)^```", section, re.MULTILINE | re.DOTALL)


def _docker_run_blocks(section: str) -> list[str]:
    """The fenced blocks that start a container directly, i.e. without the proxy."""
    return [block for block in _code_blocks(section) if "docker run" in block]


def test_direct_docker_publish_is_development_only() -> None:
    """``docker run -p 8080:8000`` publishes the app container itself, with no proxy in front."""
    docker = _section(README, "Docker")
    runs = _docker_run_blocks(docker)

    assert len(runs) == 2, "both the one-line and multiline quickstart publish the app port"
    assert all("-p 8080:8000" in run for run in runs)
    assert DEV_ONLY_MARKER in docker.lower()
    assert "compose" in docker.lower(), "the proxy-backed path must be pointed at"


def test_production_boundary_names_the_proxy_and_its_prerequisites() -> None:
    """TLS termination and authentication are the proxy's, and credentials are not shipped."""
    docker = _section(README, "Docker")

    for phrase in ("TLS", "NGINX_BASIC_AUTH", ".env"):
        assert phrase in docker, f"the Docker section must mention {phrase}"


def test_info_route_stays_public() -> None:
    """Regression: /v1/info is deliberately unauthenticated, so the boundary cannot hide it."""
    assert "/v1/info" in README
    assert re.search(r"/v1/info.*public|keep /v1/info public", README)


def test_security_policy_states_the_gateway_authenticates_nothing() -> None:
    """SECURITY.md must not imply the application enforces authentication itself."""
    auth_section = SECURITY.split("## Authentication and Authorization")[1].split("## ")[0]

    assert "reverse proxy" in auth_section.lower()
    assert "does not enforce" in auth_section.lower() or "no authentication scheme" in auth_section.lower()
