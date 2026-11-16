"""CI and coverage comments must state facts the repository can verify.

A comment that names a version or a measured percentage is a claim about another file, and it goes
stale silently: `quality.yml` set `node-version: "26"` while describing "24 matches the builder stage
of the Dockerfile", and `scripts/check_coverage.py` quoted a branch baseline that no run reproduces.
Both are checked against what they describe - the image stage, the runner's own commands - rather than
against fixed wording, so a future bump can update the claim instead of breaking the test.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
QUALITY = (PROJECT_ROOT / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")
DOCKERFILE = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
CHECK_COVERAGE = (PROJECT_ROOT / "scripts" / "check_coverage.py").read_text(encoding="utf-8")
RUN_TESTS = (PROJECT_ROOT / "scripts" / "run_tests.py").read_text(encoding="utf-8")
PYPROJECT = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
CONFORMANCE_MANIFEST = PROJECT_ROOT / "i3X" / "conformance-tests" / "package.json"


def _node_stage() -> tuple[int, str]:
    """The Node major version and image reference of the frontend stage in the Dockerfile."""
    match = re.search(r"^FROM (node:(\d+)-alpine)(?:@sha256:[0-9a-f]{64})? AS frontend$", DOCKERFILE, re.MULTILINE)
    assert match is not None, "the Dockerfile must keep a named frontend stage"
    return int(match.group(2)), match.group(1)


def _comment_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip().startswith("#")]


def test_ci_node_version_matches_the_image_stage() -> None:
    """The Node the CI installs must be the Node the image builds the frontend with."""
    major, image = _node_stage()

    versions = re.findall(r'node-version: "?(\d+)"?', QUALITY)
    assert versions, "quality.yml must pin Node explicitly"
    assert set(versions) == {str(major)}, f"CI must use Node {major} ({image}), found {sorted(set(versions))}"


def test_ci_node_comments_name_the_stage_they_claim_to_match() -> None:
    """A comment about a Node version must name the stage it is comparing against."""
    image = _node_stage()[1]

    node_comments = [line for line in _comment_lines(QUALITY) if re.search(r"\b[Nn]ode\b|\b[Dd]ockerfile\b", line)]
    assert node_comments, "the pinned Node versions should say why they are pinned"
    for line in node_comments:
        assert image in line or ">=" in line, (
            f"{line.strip()!r} must name {image} or state a minimum version, not a different version"
        )


@pytest.mark.skipif(not CONFORMANCE_MANIFEST.is_file(), reason="the i3X submodule is not checked out")
def test_conformance_node_claims_match_the_tool_manifest() -> None:
    """The conformance job's comment describes the tool, so the manifest must agree."""
    manifest = json.loads(CONFORMANCE_MANIFEST.read_text(encoding="utf-8"))

    assert manifest.get("dependencies") in (None, {}), "the tool is documented as dependency-free"
    engines = manifest.get("engines", {}).get("node", "")
    assert engines.replace(" ", "") in QUALITY.replace(" ", ""), (
        f"quality.yml must quote the tool's engine claim ({engines!r})"
    )


def test_python_image_claim_matches_the_dockerfile() -> None:
    """The matrix comment names the image the runtime ships; the Dockerfile must still name it."""
    quoted = re.findall(r"python:[0-9.]+-alpine", QUALITY)

    assert quoted, "the matrix should say which interpreter the image ships"
    for reference in quoted:
        assert reference in DOCKERFILE, f"quality.yml names {reference}, which the Dockerfile does not use"


def test_coverage_gate_comment_quotes_no_measured_percentage() -> None:
    """A quoted baseline drifts with every test added, and a drifted number is a lie in the code."""
    gate_block = CHECK_COVERAGE.split("TARGET_LINE_PERCENT")[0]

    assert not re.search(r"\d+(?:\.\d+)?\s*%", gate_block), (
        "the gate comment must describe the relationship, not a measured percentage"
    )


def test_coverage_gate_is_a_single_source_of_truth() -> None:
    """Regression: the comment claims the runner delegates the gate, so nothing else may set one."""
    assert "scripts/check_coverage.py" in RUN_TESTS, "the local runner must call the gate script"
    assert "--cov-fail-under" not in RUN_TESTS + PYPROJECT, (
        "a second line gate would contradict scripts/check_coverage.py being the only gate"
    )


def test_ruff_job_gates_lint_and_format() -> None:
    """Regression: the comment promises what docs/architecture.md requires, so both must run."""
    assert "ruff check ." in QUALITY
    assert "ruff format --check ." in QUALITY
    assert "only the lint ran" not in QUALITY, "the comment must not describe a state that no longer holds"
