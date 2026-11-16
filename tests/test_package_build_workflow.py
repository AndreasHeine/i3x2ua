"""The declared build backend must be exercised in CI, and its output validated.

``pyproject.toml`` declares ``setuptools.build_meta`` as the build backend, but until this job existed
nothing ever ran it - the published image copies raw source, so a broken packaging configuration
(`[tool.setuptools.packages.find]`, the license classifier, ``requires-python``) would ship silently.
The job builds with ``uv build`` and hands the artifacts to ``scripts/check_package_metadata.py``,
which is the same script an operator can run locally, so the CI gate and the local check cannot drift
apart.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
QUALITY = (PROJECT_ROOT / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")
JOB = re.search(r"^  package-build:$", QUALITY, re.MULTILINE)


def _job_block(job: str) -> str:
    """The YAML text of one job: up to the next job key at indent 2, or the end of the file."""
    lines = QUALITY.splitlines()
    start = next((index for index, line in enumerate(lines) if line == f"  {job}:"), None)
    assert start is not None, f"quality.yml must define a {job!r} job"
    end = next(
        (index for index in range(start + 1, len(lines)) if re.fullmatch(r"  [A-Za-z0-9_-]+:", lines[index])),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_the_quality_workflow_builds_the_package() -> None:
    """Without a build job the declared backend is never exercised."""
    assert JOB is not None, "quality.yml must define a package-build job"

    block = _job_block("package-build")
    assert "uv build" in block, "the job must build a wheel and sdist with uv build"
    assert "scripts/check_package_metadata.py" in block, "the artifacts must be validated"


def test_the_validation_script_the_job_runs_exists() -> None:
    """Regression: a workflow step may name a script that was never committed."""
    assert (PROJECT_ROOT / "scripts" / "check_package_metadata.py").is_file()


def test_the_package_build_job_needs_no_secrets_or_tokens() -> None:
    """Building and validating a wheel touches nothing external, so it holds nothing."""
    block = _job_block("package-build")

    assert "id-token" not in block, "only the coverage job may mint an OIDC token"
    assert "secrets." not in block, "a build gate must not read repository secrets"
    assert "permissions:" not in block, "the workflow-level contents: read grant is enough"


def test_the_package_build_job_bounds_and_reports_its_run() -> None:
    """Every quality job sets a timeout, and the artifacts are the evidence a reviewer inspects."""
    block = _job_block("package-build")

    assert re.search(r"timeout-minutes: \d+", block), "the job must bound its runtime"
    assert "ubuntu-26.04" in block, "quality jobs run on the pinned runner image"
    assert re.search(r"uses: actions/upload-artifact@[0-9a-f]{40}", block), "the built artifacts must be uploaded"
    assert "path: dist" in block, "the upload must carry the build output"
