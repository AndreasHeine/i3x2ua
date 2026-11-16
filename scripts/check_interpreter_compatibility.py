#!/usr/bin/env python3
"""Compare the interpreter claims of the pinned dependencies with what this repository runs.

``asyncua`` ships classifiers up to ``3.13`` and ``uvicorn`` up to ``3.14`` while ``pyproject.toml``
claims ``>=3.14,<3.16`` and the image ships ``python:3.15.2-alpine``, so both libraries run on
interpreters neither of them declares. A support claim is only a claim unless something checks it, so
this script reads the packaging metadata of the distributions CI installs and compares it with the
interpreters CI and the Dockerfile actually run:

- a distribution whose enforced ``Requires-Python`` excludes an interpreter that is run cannot be
  installed there: the run fails on that runner. That is a problem.
- the package's own ``requires-python`` floor must satisfy every dependency floor, or the range names
  an interpreter no dependency accepts. That is a problem.
- at least one interpreter CI runs must lie inside each dependency's declared classifier range. That
  interpreter is what the compatibility job in ``.github/workflows/quality.yml`` exercises, so a job
  that stops running it leaves the claim unchecked. That is a problem.
- an interpreter above a dependency's declared ceiling is reported as a note: the enforced
  ``Requires-Python`` still accepts it, and the evidence that it works is the matrix that runs it.

Usage::

    python scripts/check_interpreter_compatibility.py [--dependencies asyncua uvicorn ...]

Exit codes: 0 = every claim holds for the interpreters this repository runs, 1 = a claim contradicts
the repository configuration or a claimed distribution is not installed to read its metadata from.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"
QUALITY_PATH = PROJECT_ROOT / ".github" / "workflows" / "quality.yml"
DOCKERFILE_PATH = PROJECT_ROOT / "Dockerfile"

# The distributions whose support claims the gateway's runtime actually rides on: the OPC UA
# transport, the server loop, and the layer both request handlers go through.
DEFAULT_DEPENDENCIES = ("asyncua", "uvicorn", "fastapi", "pydantic", "pydantic-settings", "jsonschema")

_CLAUSE = re.compile(r"([<>]=?|==|!=)\s*(\d+(?:\.\d+)*)")
_REQUIRES_PYTHON = re.compile(r'requires-python\s*=\s*"([^"]+)"')
_IMAGE_INTERPRETER = re.compile(r"^FROM python:(\d+\.\d+(?:\.\d+)?)", re.MULTILINE)
_VERSION_MATRIX = re.compile(r"python-version:\s*\[([^\]]+)\]")
_VERSION_PIN = re.compile(r'python-version:\s*"?(\d+\.\d+)"?')
_MINOR_CLASSIFIER = re.compile(r"^Programming Language :: Python :: (\d+\.\d+)$")


@dataclass(frozen=True)
class DependencyClaim:
    """What a distribution says about the interpreters it supports."""

    name: str
    version: str
    requires_python: str
    declared_minors: tuple[str, ...]

    @property
    def ceiling(self) -> str | None:
        """The highest minor version the distribution declares as a classifier."""
        return max(self.declared_minors) if self.declared_minors else None


@dataclass(frozen=True)
class Report:
    """The claims that were read and how they compare with the repository."""

    claims: tuple[DependencyClaim, ...]
    interpreters: tuple[str, ...]
    image_interpreter: str | None
    problems: tuple[str, ...]
    notes: tuple[str, ...]


def _parts(version: str) -> tuple[int, int, int]:
    """A version as a fixed-length tuple, so ``3.13`` compares against ``3.13.0``."""
    parts = [int(item) for item in version.split(".")][:3]
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


def _minor(version: str) -> tuple[int, int]:
    """A version as ``(major, minor)``: classifiers declare a minor series, so ``3.15.2`` is ``3.15``."""
    parts = _parts(version)
    return (parts[0], parts[1])


def satisfies_clauses(version: str, clauses: str) -> bool:
    """Whether ``version`` satisfies every ``>=``/``<``/``==`` clause of a specifier string."""
    items = _parts(version)
    for operator, clause in _CLAUSE.findall(clauses):
        right = _parts(clause)
        if operator == ">=" and items < right:
            return False
        if operator == ">" and items <= right:
            return False
        if operator == "<=" and items > right:
            return False
        if operator == "<" and items >= right:
            return False
        if operator == "==" and items != right:
            return False
        if operator == "!=" and items == right:
            return False
    return True


def dependency_claims(names: Sequence[str]) -> list[DependencyClaim]:
    """The claims of the installed distributions, skipping ones that are not installed."""
    claims: list[DependencyClaim] = []
    for name in names:
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        declared = tuple(
            match.group(1)
            for classifier in (dist.metadata.get_all("Classifier") or [])
            if (match := _MINOR_CLASSIFIER.match(classifier))
        )
        claims.append(
            DependencyClaim(
                name=name,
                version=dist.version,
                requires_python=dist.metadata.get("Requires-Python") or "",
                declared_minors=declared,
            )
        )
    return claims


def project_requires_python(path: Path = PYPROJECT_PATH) -> str:
    """The interpreter range ``pyproject.toml`` declares."""
    return _REQUIRES_PYTHON.search(path.read_text(encoding="utf-8")).group(1)  # type: ignore[union-attr]


def repository_interpreters(path: Path = QUALITY_PATH) -> tuple[str, ...]:
    """Every interpreter CI sets up: matrix entries and single pins, deduplicated."""
    text = path.read_text(encoding="utf-8")
    found: set[str] = set()
    for matrix in _VERSION_MATRIX.findall(text):
        found.update(item.strip().strip("\"'") for item in matrix.split(","))
    for pin in _VERSION_PIN.findall(text):
        found.add(pin)
    return tuple(sorted(found))


def image_interpreter(path: Path = DOCKERFILE_PATH) -> str | None:
    """The interpreter the published image ships."""
    match = _IMAGE_INTERPRETER.search(path.read_text(encoding="utf-8"))
    return match.group(1) if match is not None else None


def evaluate(
    claims: Sequence[DependencyClaim],
    *,
    project_requires_python: str,
    interpreters: Sequence[str],
    image_interpreter: str | None,
) -> Report:
    """Compare the claims with the interpreters this repository installs and ships."""
    problems: list[str] = []
    notes: list[str] = []
    run_interpreters = [*interpreters, *([image_interpreter] if image_interpreter else [])]
    floor = _floor(project_requires_python)

    for claim in claims:
        for interpreter in run_interpreters:
            if claim.requires_python and not satisfies_clauses(interpreter, claim.requires_python):
                problems.append(
                    f"{claim.name} {claim.version} requires {claim.requires_python}, "
                    f"which excludes {interpreter} (run by CI or shipped by the image)"
                )
        ceiling = claim.ceiling
        if ceiling is not None:
            floor_claim = f">={min(claim.declared_minors)},<={ceiling}"
            if not any(satisfies_clauses(interpreter, floor_claim) for interpreter in interpreters):
                problems.append(
                    f"{claim.name} {claim.version} declares support for {min(claim.declared_minors)}-{ceiling}, "
                    f"and none of the interpreters CI runs ({', '.join(sorted(interpreters))}) is inside it: "
                    f"add an interpreter compatibility job for {ceiling}"
                )
            if image_interpreter and _minor(image_interpreter) > _minor(ceiling):
                notes.append(
                    f"{claim.name} {claim.version} declares classifiers up to {ceiling}; the image ships "
                    f"{image_interpreter}, beyond that claim - its enforced {claim.requires_python or 'metadata'} "
                    f"accepts it and the matrix jobs run it"
                )
        if claim.requires_python and floor is not None and not satisfies_clauses(floor, claim.requires_python):
            problems.append(
                f"the package declares requires-python {project_requires_python}, whose floor {floor} "
                f"is rejected by {claim.name} {claim.version} ({claim.requires_python})"
            )

    return Report(
        claims=tuple(claims),
        interpreters=tuple(interpreters),
        image_interpreter=image_interpreter,
        problems=tuple(problems),
        notes=tuple(notes),
    )


def _floor(requires_python: str) -> str | None:
    """The lowest version a ``requires-python`` range admits, i.e. its ``>=`` clause."""
    match = re.search(r">=\s*(\d+(?:\.\d+)*)", requires_python)
    return match.group(1) if match is not None else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--dependencies", nargs="*", default=list(DEFAULT_DEPENDENCIES))
    args = parser.parse_args(argv)

    claims = dependency_claims(args.dependencies)
    missing = [name for name in args.dependencies if name not in {claim.name for claim in claims}]
    report = evaluate(
        claims,
        project_requires_python=project_requires_python(),
        interpreters=repository_interpreters(),
        image_interpreter=image_interpreter(),
    )

    print(f"Interpreters CI runs: {', '.join(report.interpreters)}")
    print(f"Interpreter the image ships: {report.image_interpreter or 'none found in the Dockerfile'}")
    for claim in report.claims:
        declared = "/".join(claim.declared_minors) if claim.declared_minors else "no minor classifiers"
        print(f"{claim.name} {claim.version}: requires-python {claim.requires_python or 'unset'}; {declared}")
    for note in report.notes:
        print(f"note: {note}")
    for problem in missing:
        print(f"problem: {problem} is not installed, so its claim cannot be read")
    for problem in report.problems:
        print(f"problem: {problem}")

    if report.problems or missing:
        return 1
    print("Every dependency claim holds for the interpreters this repository runs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
