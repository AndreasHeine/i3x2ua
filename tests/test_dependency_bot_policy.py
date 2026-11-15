"""Dependency updates are owned by one bot, and asyncua is never upgraded automatically.

The transport adapter reaches into asyncua's private internals
(``infrastructure/opcua/recovery.py`` overrides ``_send_request`` / ``_request_semaphore`` /
``_supervisor_task`` and reads ``_supervisor_owns_requests``), and the OPC UA fixture server does the
same (``conf-test-server/server.py``). A minor or major bump can therefore break reconnect and
subscription resync while every test still passes, so the bot must not propose those upgrades: they
are a human decision made against the conformance suite. Two bots configured for the same ecosystems
produced a duplicate PR stream for the same dependency, so one bot owns dependency updates.

Vulnerability detection is deliberately not part of this file: ``pip-audit`` and the Trivy image scan
run in CI against the PyPI advisory database, which is what SECURITY.md's claim rests on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import tomllib

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RENOVATE = PROJECT_ROOT / ".github" / "renovate.json"
DEPENDABOT = PROJECT_ROOT / ".github" / "dependabot.yml"
SECURITY = (PROJECT_ROOT / "SECURITY.md").read_text(encoding="utf-8")
PYPROJECT = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

# The library whose internals the gateway depends on.
GUARDED_PACKAGE = "asyncua"

# Managers a single bot must cover: Python pins and lock, the image's base stage, compose images,
# and the pinned actions in .github/workflows. Dropping a bot must not leave any of them unmanaged.
REQUIRED_MANAGERS = {"pep621", "dockerfile", "docker-compose", "github-actions", "npm"}


def _renovate_config() -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(RENOVATE.read_text(encoding="utf-8"))
    return parsed


def _rules_matching(config: dict[str, Any], package: str) -> list[dict[str, Any]]:
    """Every ``packageRules`` entry that names ``package``."""
    rules = config.get("packageRules") or []
    return [rule for rule in rules if package in (rule.get("matchPackageNames") or [])]


def _blocked_update_types(config: dict[str, Any], package: str) -> set[str]:
    """Update types the bot must not propose for ``package``."""
    blocked: set[str] = set()
    for rule in _rules_matching(config, package):
        if rule.get("enabled") is False:
            types = rule.get("matchUpdateTypes") or ["major", "minor", "patch"]
            blocked |= set(types)
    return blocked


def test_one_dependency_bot_is_shipped() -> None:
    """Two bots on the same ecosystem write duplicate PRs for the same dependency."""
    assert RENOVATE.is_file(), "the repository must configure exactly one dependency bot"
    assert not DEPENDABOT.is_file(), (
        f"{DEPENDABOT.relative_to(PROJECT_ROOT)} must be removed: two bots on the same ecosystems "
        "produce duplicate upgrade PRs, and the single bot's rules are the ones that guard asyncua"
    )


def test_the_single_bot_covers_every_dependency_source() -> None:
    """Regression: removing a bot must not leave any dependency source unmanaged."""
    managers = set(_renovate_config().get("enabledManagers") or [])

    assert REQUIRED_MANAGERS <= managers, f"the bot must still manage {REQUIRED_MANAGERS - managers}"


def test_asyncua_major_and_minor_upgrades_are_not_proposed() -> None:
    """A minor or major bump can change the internals the reconnect machinery overrides."""
    blocked = _blocked_update_types(_renovate_config(), GUARDED_PACKAGE)

    assert {"major", "minor"} <= blocked, f"{GUARDED_PACKAGE} major/minor upgrades must be blocked"


def test_asyncua_is_not_moved_by_lockfile_maintenance() -> None:
    """A lockfile refresh would move the pin without naming it in a package-rule PR."""
    blocked = _blocked_update_types(_renovate_config(), GUARDED_PACKAGE)

    assert "lockfileMaintenance" in blocked, f"lockFileMaintenance must not move {GUARDED_PACKAGE} inside the lockfile"


def test_no_dependency_pr_is_automerged() -> None:
    """Every upgrade must be reviewed and tested before it lands."""
    config = _renovate_config()

    assert config.get("automerge") is not True, "the bot must never auto-merge dependency PRs"
    assert config.get("lockFileMaintenance", {}).get("automerge") is not True, (
        "lockfile maintenance PRs must not be auto-merged"
    )
    assert all(rule.get("automerge") is not True for rule in config.get("packageRules") or []), (
        "no package rule may enable auto-merge"
    )


def test_asyncua_pin_stays_exact() -> None:
    """Regression: the guard assumes a pin, not a range that a lockfile refresh can widen."""
    pinned = [spec for spec in PYPROJECT["project"]["dependencies"] if spec.startswith(f"{GUARDED_PACKAGE}==")]

    assert pinned, f"{GUARDED_PACKAGE} must stay pinned exactly, not ranged"


def test_security_policy_names_one_bot_and_the_asyncua_guard() -> None:
    """SECURITY.md must describe the ownership it ships, not both bots."""
    section = SECURITY.split("### Dependency Management")[1].split("## ")[0]

    assert "Renovate" in section
    assert "Dependabot" not in section, "SECURITY.md must not claim a bot the repository does not ship"
    assert GUARDED_PACKAGE in section, "SECURITY.md must state which dependency is not upgraded automatically"
