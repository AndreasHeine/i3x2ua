"""One CodeQL workflow, with the language coverage the duplicate silently provided.

The repository shipped two scanners: `codeql-analysis.yml` (python, javascript-typescript, with the
repository's own config) and the GitHub-generated `codeql.yml` (actions, javascript-typescript,
python, without it). Both ran on every push and PR, so each finding was reported twice and every
analysis cost doubled - while the generated workflow also scanned the vendored i3X submodule, which
the config deliberately excludes. Removing it must therefore keep the `actions` language it was the
only one to cover, otherwise workflow injection stops being scanned at all.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = PROJECT_ROOT / ".github" / "workflows"
CONFIG = (PROJECT_ROOT / ".github" / "codeql" / "codeql-config.yml").read_text(encoding="utf-8")

# The languages the gateway and its deployment surface need scanned.
REQUIRED_LANGUAGES = {"python", "javascript-typescript", "actions"}

INIT_ACTION = "github/codeql-action/init"
_CONFIG_FILE = "./.github/codeql/codeql-config.yml"

# A ``language:``/``languages:`` value: a list, a comma list, or one entry per matrix include.
_LANGUAGE_LINE = re.compile(r"^\s*(?:-\s+)?languages?\s*:\s*(.+)$", re.MULTILINE)


def _workflow_texts() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(WORKFLOW_DIR.glob("*.yml"))}


def _scanner() -> tuple[str, str]:
    """The single workflow that runs CodeQL analysis, i.e. the one calling ``codeql-action/init``."""
    scanners = {name: text for name, text in _workflow_texts().items() if INIT_ACTION in text}

    assert len(scanners) == 1, f"only one CodeQL workflow may scan: found {sorted(scanners)}"
    return scanners.popitem()


def _scan_languages(text: str) -> set[str]:
    """Every language a workflow's matrix or init step declares."""
    languages: set[str] = set()
    for match in _LANGUAGE_LINE.finditer(text):
        value = match.group(1).strip()
        items = value.strip("[]").split(",") if value.startswith("[") else value.split(",")
        for item in items:
            entry = item.strip()
            # `${{ matrix.language }}` names the matrix, not a language; the matrix lines carry it.
            if entry and not entry.startswith("${{"):
                languages.add(entry)
    return languages


def test_the_generated_duplicate_workflow_is_gone() -> None:
    """Two scanners report the same finding twice and double the analysis cost per push."""
    assert not (WORKFLOW_DIR / "codeql.yml").is_file(), (
        "the GitHub-generated CodeQL workflow duplicates the repository's own scanner"
    )
    _scanner()


def test_the_scanner_covers_every_required_language() -> None:
    """Regression: the duplicate was the only workflow scanning Actions workflow files."""
    name, text = _scanner()
    missing = REQUIRED_LANGUAGES - _scan_languages(text)

    assert missing == set(), f"{name} must still scan {sorted(missing)}"


def test_the_scanner_uses_the_repository_codeql_config() -> None:
    """Without the config the vendored i3X submodule is scanned again: findings nobody can fix."""
    _, text = _scanner()

    assert f"config-file: {_CONFIG_FILE}" in text, "the scanner must use the repository's CodeQL config"


def test_the_config_still_excludes_third_party_material() -> None:
    """The exclusion is what makes the single scanner's findings actionable."""
    ignored = re.findall(r"^\s*-\s*(\S+)$", CONFIG.split("paths-ignore:")[1], re.MULTILINE)

    assert "i3X" in ignored, "the vendored i3X submodule must stay out of the scan"


def test_the_scanner_runs_on_a_schedule() -> None:
    """A schedule catches advisories in code no PR touched; the duplicate's schedule must survive."""
    _, text = _scanner()

    assert "schedule:" in text, "CodeQL must keep a scheduled scan"


def test_the_scanner_can_publish_findings() -> None:
    """``security-events: write`` is what makes the analysis visible; a scoped-down run reports nothing."""
    _, text = _scanner()

    assert re.search(r"security-events:\s*write", text), "CodeQL must be allowed to write security events"
