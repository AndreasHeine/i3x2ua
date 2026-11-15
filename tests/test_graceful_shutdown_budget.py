"""The SIGTERM budget must be large enough for the cleanup the gateway performs.

A termination signal runs three phases: uvicorn stops accepting connections and waits for
in-flight requests, then the lifespan closes the runtime (cancels and awaits the background
tasks, releases monitored nodes with upstream round-trips, drains MCP/SSE sessions, disconnects
the upstream - ``bootstrap/runtime_setup.py``). Both phases were unconfigured: uvicorn's
``timeout_graceful_shutdown`` defaults to ``None``, and Docker sends SIGKILL 10s after SIGTERM,
so a cleanup that waits on an upstream round-trip could be killed mid-way and leave a
subscription or session half-closed. The budget is therefore written down in the image, the
compose service, and ``run.py``, and the ordering of the numbers is asserted here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from i3x_server.config.settings import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
COMPOSE = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
README = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
ENV_EXAMPLE = (PROJECT_ROOT / ".env-example").read_text(encoding="utf-8")
ENTRYPOINT = (PROJECT_ROOT / "nginx" / "entrypoint.sh").read_text(encoding="utf-8")

DRAIN_FLAG = "--timeout-graceful-shutdown"
# The lifespan cleanup runs after uvicorn's in-flight wait and is not covered by it, so the
# container budget needs slack on top of the drain: node releases and the upstream disconnect
# are bounded by the upstream request timeout (10s), rounded up.
CLEANUP_MARGIN_SECONDS = 15


def _dockerfile_cmd(dockerfile: str) -> list[str]:
    """The JSON ``CMD`` array the published image starts with."""
    line = next(line for line in dockerfile.splitlines() if line.startswith("CMD "))
    return [str(item) for item in json.loads(line[len("CMD ") :])]


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
        if not stripped.startswith("- "):
            break
        value = re.sub(r"\s+#.*$", "", stripped[2:])
        items.append(value.strip('"').strip("'"))
    return items


def _flag_value(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


def _drain_seconds(args: list[str]) -> int:
    """The configured drain, asserted present: an unbounded wait is what this item removes."""
    value = _flag_value(args, DRAIN_FLAG)
    assert value is not None, f"{DRAIN_FLAG} must be part of the server command"
    return int(value)


def _stop_grace_seconds(block: str) -> int:
    match = re.search(r"^\s*stop_grace_period:\s*(\d+)s\s*$", block, re.MULTILINE)
    assert match is not None, "stop_grace_period must be configured for the gateway service"
    return int(match.group(1))


def test_image_command_drains_in_flight_requests() -> None:
    """The published image bounds uvicorn's wait for in-flight requests."""
    drain = _drain_seconds(_dockerfile_cmd(DOCKERFILE))

    assert drain > 0


def test_compose_command_drains_with_the_same_budget_as_the_image() -> None:
    """The compose override replaces the image CMD, so it must carry the same drain.

    ``command:`` rewrites the whole command line, so a flag that only lives in the Dockerfile
    would silently disappear from the shipped deployment.
    """
    command = _yaml_sequence(_service_block(COMPOSE, "i3x2ua"), "command")

    assert _drain_seconds(command) == _drain_seconds(_dockerfile_cmd(DOCKERFILE))


def test_container_grace_period_covers_the_drain_and_the_lifespan_cleanup() -> None:
    """Docker's default 10s grace kills cleanup that the drain alone cannot cover.

    The lifespan shutdown happens *after* uvicorn's in-flight wait, so the container budget is
    the drain plus slack for the task cancellation, node releases and upstream disconnect.
    """
    block = _service_block(COMPOSE, "i3x2ua")
    drain = _drain_seconds(_yaml_sequence(block, "command"))

    assert _stop_grace_seconds(block) >= drain + CLEANUP_MARGIN_SECONDS


def test_drain_leaves_the_proxy_time_to_finish_a_drained_request() -> None:
    """nginx must not time out a request the gateway is still draining."""
    timeouts = [int(value) for value in re.findall(r"proxy_read_timeout\s+(\d+)s", ENTRYPOINT)]
    assert timeouts

    assert _drain_seconds(_dockerfile_cmd(DOCKERFILE)) <= min(timeouts)


def test_drain_covers_the_upstream_round_trip_the_cleanup_waits_for() -> None:
    """In-flight tasks are bounded by the upstream request timeout, so the drain must exceed it."""
    assert _drain_seconds(_dockerfile_cmd(DOCKERFILE)) >= get_settings().opcua_reconnect_request_timeout_seconds


@pytest.mark.parametrize(
    ("name", "block"),
    [("i3x2ua", _service_block(COMPOSE, "i3x2ua")), ("nginx", _service_block(COMPOSE, "nginx"))],
)
def test_services_that_hold_state_have_a_grace_period(name: str, block: str) -> None:
    """Anything with cleanup to finish must outlive the default kill, not just the gateway."""
    assert _stop_grace_seconds(block) > 10, f"{name} must set stop_grace_period above Docker's default"


def test_readme_documents_the_orchestrator_budget() -> None:
    """Kubernetes and plain Docker do not read compose, so the budget has to be written down."""
    match = re.search(r"terminationGracePeriodSeconds[:=]\s*(\d+)", README)
    assert match is not None, "README must document terminationGracePeriodSeconds"

    documented = int(match.group(1))
    assert documented >= _stop_grace_seconds(_service_block(COMPOSE, "i3x2ua"))


def test_env_example_documents_the_tunable() -> None:
    """Every uvicorn tunable run.py reads is listed in the env template."""
    assert "I3X_UVICORN_GRACEFUL_SHUTDOWN_SECONDS" in ENV_EXAMPLE
