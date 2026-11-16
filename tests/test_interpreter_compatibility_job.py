"""The interpreters the pinned libraries claim to support must actually be exercised.

``asyncua==2.1.0`` ships classifiers up to **3.13** and ``uvicorn==0.54.0`` up to **3.14**, while
``pyproject.toml`` claims ``>=3.14,<3.16`` and the image ships ``python:3.15.2-alpine`` - so the
gateway runs both libraries on interpreters neither of them declares. That gap is only checkable if
CI runs the highest interpreter both libraries do claim, and if the claims are compared with what the
repository runs. The job cannot install the project there (``uv sync --frozen`` refuses an
interpreter outside ``requires-python``), so it installs the dependency list from ``pyproject.toml``
and runs the suite without the project itself; the claim comparison is the same script an operator
can run locally.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.check_interpreter_compatibility import (
    DependencyClaim,
    dependency_claims,
    evaluate,
    image_interpreter,
    repository_interpreters,
    satisfies_clauses,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
QUALITY = (PROJECT_ROOT / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")
DOCKERFILE = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
PYPROJECT = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
PYPROJECT_REQUIRES = re.search(r'requires-python = "([^"]+)"', PYPROJECT).group(1)  # type: ignore[union-attr]
JOB = "interpreter-compatibility"

# The libraries the gateway reaches into most: asyncua's internals are used by recovery.py, and
# uvicorn owns the server loop the SSE streams run on.
CLAIMED: tuple[str, ...] = ("asyncua", "uvicorn")


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


def _claimed_ceiling() -> str:
    """The highest interpreter every one of the two libraries declares support for."""
    claims = dependency_claims(CLAIMED)
    ceilings = [max(claim.declared_minors) for claim in claims]
    assert ceilings, "the claimed distributions must be installed for the claims to be readable"
    return min(ceilings)


def test_the_workflow_runs_the_interpreter_the_libraries_claim() -> None:
    """Neither library declares the shipped interpreter, so the interpreter they do declare runs too."""
    block = _job_block(JOB)

    assert f'python-version: "{_claimed_ceiling()}"' in block, (
        f"the job must run the ceiling {CLAIMED} declare, not a version chosen by hand"
    )
    assert "timeout-minutes: " in block, "quality jobs bound their runtime"
    assert "ubuntu-26.04" in block, "quality jobs run on the pinned runner image"


def test_the_job_installs_the_declared_dependencies_without_the_project() -> None:
    """``uv sync --frozen`` resolves against ``requires-python``, which excludes that interpreter."""
    block = _job_block(JOB)

    assert "-r pyproject.toml" in block, "the dependency list must come from pyproject.toml, not a copied pin list"
    assert "run: uv sync" not in block, "the project itself cannot be installed outside requires-python"


def test_the_job_runs_the_suite_and_the_claim_check() -> None:
    """The libraries' claim is only verified if the gateway's transport and server paths run there."""
    block = _job_block(JOB)

    assert re.search(r"python -m pytest", block), "the suite must exercise the asyncua and uvicorn paths"
    assert "scripts/check_interpreter_compatibility.py" in block, "the claims must be compared with what runs"
    assert "id-token" not in block, "only the coverage job may mint an OIDC token"


def test_the_job_comment_quotes_the_declared_range() -> None:
    """Regression: the comment explains why the interpreter is below the project's floor."""
    block = _job_block(JOB)
    declared = re.search(r'requires-python = "([^"]+)"', PYPROJECT)
    assert declared is not None, "pyproject must declare requires-python"
    assert declared.group(1) in block, "the comment must quote the range pyproject declares"


def test_the_claimed_interpreter_is_not_added_to_the_coverage_matrix() -> None:
    """The matrix installs the project, so it may only contain interpreters requires-python allows."""
    matrix = re.search(r"python-version: \[([^\]]+)\]", _job_block("pytest"))
    assert matrix is not None, "the pytest job must keep its interpreter matrix"
    claimed = _claimed_ceiling()
    assert claimed not in matrix.group(1), f"{claimed} is outside requires-python and cannot install the project"


def test_the_claim_script_exists() -> None:
    """Regression: a workflow step may name a script that was never committed."""
    assert (PROJECT_ROOT / "scripts" / "check_interpreter_compatibility.py").is_file()


def test_the_pinned_claims_hold_for_the_interpreters_this_repository_runs() -> None:
    """The real claims against the real repository configuration: nothing may be unrunnable."""
    interpreters = repository_interpreters()
    image = image_interpreter()
    report = evaluate(
        dependency_claims(tuple(sorted({*CLAIMED, "fastapi", "pydantic", "jsonschema", "pydantic-settings"}))),
        project_requires_python=PYPROJECT_REQUIRES,
        interpreters=interpreters,
        image_interpreter=image,
    )
    assert report.problems == (), f"the dependencies reject an interpreter this repository runs: {report.problems}"
    assert _claimed_ceiling() in interpreters, "CI must run an interpreter inside the libraries' declared range"


def test_a_dependency_that_rejects_a_run_interpreter_is_a_problem() -> None:
    """A library whose Requires-Python excludes an interpreter CI runs cannot be installed there."""
    report = evaluate(
        [DependencyClaim(name="asyncua", version="2.1.0", requires_python=">=3.14", declared_minors=("3.14",))],
        project_requires_python=PYPROJECT_REQUIRES,
        interpreters=("3.13", "3.14", "3.15"),
        image_interpreter="3.15.2",
    )
    assert any("asyncua" in problem and "3.13" in problem for problem in report.problems)


def test_an_unrun_declared_interpreter_is_a_problem() -> None:
    """The job this item asks for is what keeps the claim honest; dropping it must fail the check."""
    report = evaluate(
        [DependencyClaim(name="asyncua", version="2.1.0", requires_python=">=3.10", declared_minors=("3.10", "3.13"))],
        project_requires_python=PYPROJECT_REQUIRES,
        interpreters=("3.14", "3.15"),
        image_interpreter="3.15.2",
    )
    assert any("asyncua" in problem and "compatibility" in problem for problem in report.problems)


def test_a_floor_below_a_dependency_floor_is_a_problem() -> None:
    """The package may not claim an interpreter a dependency rejects, or installs fail half-way."""
    report = evaluate(
        [DependencyClaim(name="uvicorn", version="0.54.0", requires_python=">=3.16", declared_minors=("3.16",))],
        project_requires_python=PYPROJECT_REQUIRES,
        interpreters=("3.14", "3.15"),
        image_interpreter="3.15.2",
    )
    assert any("uvicorn" in problem and "requires-python" in problem for problem in report.problems)


def test_a_ceiling_below_the_shipped_interpreter_is_reported_not_failed() -> None:
    """The shipped interpreter is beyond the libraries' own claim: that is a note, not a broken gate."""
    report = evaluate(
        [DependencyClaim(name="asyncua", version="2.1.0", requires_python=">=3.10", declared_minors=("3.10", "3.13"))],
        project_requires_python=PYPROJECT_REQUIRES,
        interpreters=("3.13", "3.14", "3.15"),
        image_interpreter="3.15.2",
    )
    assert report.problems == (), f"the claims are enforced, so nothing should fail: {report.problems}"
    assert any("asyncua" in note and "3.15" in note for note in report.notes)


@pytest.mark.parametrize("interpreter", ["3.13", "3.14", "3.15", "3.15.2"])
def test_the_image_and_matrix_interpreters_satisfy_every_dependency_claim(interpreter: str) -> None:
    """Every interpreter named in the Dockerfile or CI must be installable for both libraries."""
    claims = dependency_claims(CLAIMED)
    assert claims, "the claimed distributions must be installed"
    for claim in claims:
        assert satisfies_clauses(interpreter, claim.requires_python), f"{claim.name} rejects {interpreter}"


def test_the_dockerfile_interpreter_is_named_by_the_report() -> None:
    """The report must name the interpreter the image ships, so the ceiling gap is visible."""
    shipped = image_interpreter()
    assert shipped is not None, "the Dockerfile must name the interpreter the image ships"
    report = evaluate(
        [DependencyClaim(name="uvicorn", version="0.54.0", requires_python=">=3.10", declared_minors=("3.10", "3.14"))],
        project_requires_python=PYPROJECT_REQUIRES,
        interpreters=("3.13", "3.14", "3.15"),
        image_interpreter=shipped,
    )
    assert shipped in DOCKERFILE
    assert any(shipped in note for note in report.notes)
