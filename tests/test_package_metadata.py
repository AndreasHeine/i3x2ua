"""The published wheel and sdist must carry the metadata ``pyproject.toml`` declares.

Nothing in CI built a distribution before this job existed, so the declared setuptools backend
(``[build-system]``) was never exercised: a packaging mistake - a dropped subpackage, a drifted
``requires-python``, a license that stops shipping - would only be noticed by whoever installs the
published package. The validator compares the built artifacts with ``pyproject.toml`` and with the
repository's own source tree, so an artifact cannot disagree with the repository it came from.

Fixtures are written the way setuptools writes them: specifier clauses are reordered
(``<3.16,>=3.14``, ``uvicorn<0.55,>=0.54``), so the validator must compare requirement clauses,
not raw strings.
"""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import pytest
import tomllib

from scripts.check_package_metadata import check_package

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
PROJECT = PYPROJECT["project"]
NAME = str(PROJECT["name"])
VERSION = str(PROJECT["version"])
DESCRIPTION = str(PROJECT["description"])
REQUIRES_PYTHON = str(PROJECT["requires-python"])
CLASSIFIERS = [str(classifier) for classifier in PROJECT["classifiers"]]
DEPENDENCIES = [str(dependency) for dependency in PROJECT["dependencies"]]
EXTRAS = {
    str(extra): [str(requirement) for requirement in requirements]
    for extra, requirements in PROJECT["optional-dependencies"].items()
}
LICENSE_TEXT = (PROJECT_ROOT / "LICENSE").read_bytes()
PACKAGE = "i3x_server"
SOURCE_MODULES = [f"{PACKAGE}/__init__.py", f"{PACKAGE}/config/settings.py"]


def _normalized(requirement: str) -> str:
    """A requirement as the build backend writes it: name and extras, then sorted specifier clauses."""
    positions = (position for separator in ("<", ">", "=", "!", "~") if (position := requirement.find(separator)) != -1)
    index = min(positions, default=-1)
    if index == -1:
        return requirement
    return f"{requirement[:index]}{','.join(sorted(requirement[index:].split(',')))}"


def _metadata(
    *,
    name: str = NAME,
    version: str = VERSION,
    summary: str = DESCRIPTION,
    requires_python: str = REQUIRES_PYTHON,
    classifiers: list[str] = CLASSIFIERS,
    dependencies: list[str] = DEPENDENCIES,
    extras: dict[str, list[str]] = EXTRAS,
    license_file: str | None = "LICENSE",
) -> str:
    """METADATA/PKG-INFO headers in the shape the build backend emits."""
    lines = [
        "Metadata-Version: 2.4",
        f"Name: {name}",
        f"Version: {version}",
        f"Summary: {summary}",
        *(f"Classifier: {classifier}" for classifier in classifiers),
        f"Requires-Python: {requires_python}",
        *(["License-File: LICENSE"] if license_file else []),
        *(f"Requires-Dist: {_normalized(dependency)}" for dependency in dependencies),
        *(f"Provides-Extra: {extra}" for extra in extras),
        *(
            f'Requires-Dist: {_normalized(requirement)}; extra == "{extra}"'
            for extra, requirements in extras.items()
            for requirement in requirements
        ),
    ]
    return "\n".join(lines) + "\n"


def _build_dist(tmp_path: Path, *, wheel_modules: list[str], overrides: dict[str, Any]) -> Path:
    """A ``dist/`` directory holding one wheel and one sdist for the given source modules."""
    dist = tmp_path / "dist"
    dist.mkdir()

    wheel_version: str = overrides.get("wheel_version", VERSION)
    sdist_version: str = overrides.get("sdist_version", VERSION)
    wheel_tag: str = overrides.get("wheel_tag", "py3-none-any")
    top_level: str = overrides.get("top_level", PACKAGE)
    wheel_metadata: str = overrides.get("wheel_metadata", _metadata())
    sdist_metadata: str = overrides.get("sdist_metadata", wheel_metadata)
    modules: list[str] = overrides.get("wheel_modules", wheel_modules)

    dist_info = f"{NAME}-{wheel_version}.dist-info"
    wheel_entries: dict[str, bytes] = {
        f"{dist_info}/METADATA": wheel_metadata.encode(),
        f"{dist_info}/WHEEL": f"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: {wheel_tag}\n".encode(),
        f"{dist_info}/top_level.txt": f"{top_level}\n".encode(),
    }
    if overrides.get("wheel_license", True):
        wheel_entries[f"{dist_info}/licenses/LICENSE"] = LICENSE_TEXT
    for module in modules:
        wheel_entries[module] = b""
    wheel_entries.update(overrides.get("wheel_files", {}))

    with zipfile.ZipFile(dist / f"{NAME}-{wheel_version}-{wheel_tag}.whl", "w") as wheel:
        for path, payload in wheel_entries.items():
            wheel.writestr(path, payload)

    root = f"{NAME}-{sdist_version}"
    sdist_entries: dict[str, bytes] = {f"{root}/PKG-INFO": sdist_metadata.encode(), f"{root}/pyproject.toml": b""}
    if overrides.get("sdist_license", True):
        sdist_entries[f"{root}/LICENSE"] = LICENSE_TEXT
    for module in modules:
        sdist_entries[f"{root}/{module}"] = b""
    sdist_entries.update(overrides.get("sdist_files", {}))

    with tarfile.open(dist / f"{NAME}-{sdist_version}.tar.gz", "w:gz") as sdist:
        for path, payload in sdist_entries.items():
            info = tarfile.TarInfo(path)
            info.size = len(payload)
            sdist.addfile(info, io.BytesIO(payload))

    return dist


def _source_root(tmp_path: Path, modules: list[str]) -> Path:
    """A repository tree whose package holds exactly the given modules."""
    root = tmp_path / "repo"
    for module in modules:
        path = root / module
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    (root / "LICENSE").write_bytes(LICENSE_TEXT)
    text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    (root / "pyproject.toml").write_text(text, encoding="utf-8")
    return root


def _check(dist: Path, source: Path) -> int:
    return check_package(dist, pyproject=source / "pyproject.toml", source_root=source)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"wheel_files": {"frontend/index.html": b"<html>"}}, "outside"),
        ({"wheel_files": {"tests/test_api.py": b""}}, "outside"),
        ({"wheel_files": {"README.md": b"x"}}, "outside"),
        ({"wheel_modules": [f"{PACKAGE}/config/settings.py"]}, "missing from the wheel"),
        ({"wheel_license": False}, "LICENSE"),
        ({"sdist_license": False}, "LICENSE"),
        ({"wheel_version": "1.17.0"}, "version"),
        ({"sdist_version": "1.17.0"}, "disagree"),
        ({"wheel_metadata": _metadata(version="1.17.0")}, "version"),
        ({"wheel_metadata": _metadata(name="i3x-gateway")}, "name"),
        ({"wheel_metadata": _metadata(summary="A different description")}, "summary"),
        ({"wheel_metadata": _metadata(requires_python=">=3.13,<3.17")}, "requires-python"),
        ({"wheel_metadata": _metadata(classifiers=[])}, "license classifier"),
        ({"wheel_metadata": _metadata(dependencies=[*DEPENDENCIES, "requests>=2.0"])}, "dependencies"),
        (
            {
                "wheel_metadata": _metadata(dependencies=["asyncua==2.0.0", *DEPENDENCIES[1:]]),
                "sdist_metadata": _metadata(),
            },
            "dependencies",
        ),
        ({"wheel_metadata": _metadata(extras={})}, "extras"),
        ({"wheel_metadata": _metadata(license_file=None)}, "license file"),
        ({"top_level": "tests"}, "top-level"),
        ({"wheel_tag": "cp314-cp314-linux_x86_64"}, "pure"),
    ],
)
def test_a_distribution_that_deviates_from_the_declared_metadata_is_rejected(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    overrides: dict[str, Any],
    expected: str,
) -> None:
    """Every way a published artifact can drift from pyproject.toml must fail the gate."""
    source = _source_root(tmp_path, SOURCE_MODULES)
    dist = _build_dist(tmp_path, wheel_modules=SOURCE_MODULES, overrides=overrides)

    assert _check(dist, source) == 1
    assert expected in capsys.readouterr().out


def test_a_distribution_matching_the_declared_metadata_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The point of the gate is a passing build, not merely a loud failure."""
    source = _source_root(tmp_path, SOURCE_MODULES)
    dist = _build_dist(tmp_path, wheel_modules=SOURCE_MODULES, overrides={})

    assert _check(dist, source) == 0
    assert "metadata gates passed" in capsys.readouterr().out


def test_reordered_specifier_clauses_are_accepted(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Regression: the backend sorts clauses, so ``<3.16,>=3.14`` must not read as drift."""
    source = _source_root(tmp_path, SOURCE_MODULES)
    dist = _build_dist(
        tmp_path,
        wheel_modules=SOURCE_MODULES,
        overrides={"wheel_metadata": _metadata(requires_python="<3.16,>=3.14")},
    )

    assert _check(dist, source) == 0
    assert "metadata gates passed" in capsys.readouterr().out


def test_reordered_dependency_clauses_are_accepted(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Regression: ``uvicorn>=0.54,<0.55`` is written ``uvicorn<0.55,>=0.54``.

    The split point must be the earliest specifier character, not the first separator tried, or the
    name becomes ``uvicorn>=0.54,`` and every build reads as a dependency drift.
    """
    assert _normalized("uvicorn>=0.54,<0.55") == "uvicorn<0.55,>=0.54"

    source = _source_root(tmp_path, SOURCE_MODULES)
    dist = _build_dist(
        tmp_path,
        wheel_modules=SOURCE_MODULES,
        overrides={"wheel_metadata": _metadata(dependencies=[*DEPENDENCIES[:-1], "uvicorn>=0.54,<0.55"])},
    )

    assert _check(dist, source) == 0
    assert "metadata gates passed" in capsys.readouterr().out


def test_the_sdist_metadata_is_read_from_the_top_level_pkg_info(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression: setuptools writes a second PKG-INFO under ``*.egg-info``, with older metadata."""
    stale = _metadata(version="0.9.0", dependencies=["asyncua==2.0.0"]).encode()
    source = _source_root(tmp_path, SOURCE_MODULES)
    dist = _build_dist(
        tmp_path,
        wheel_modules=SOURCE_MODULES,
        overrides={"sdist_files": {f"{NAME}-{VERSION}/i3x2ua.egg-info/PKG-INFO": stale}},
    )

    assert _check(dist, source) == 0
    assert "metadata gates passed" in capsys.readouterr().out


def test_the_gate_requires_exactly_one_wheel_and_one_sdist(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A stale wheel left in ``dist/`` would let an old build satisfy the gate."""
    source = _source_root(tmp_path, SOURCE_MODULES)
    dist = _build_dist(tmp_path, wheel_modules=SOURCE_MODULES, overrides={})
    built = (dist / f"{NAME}-{VERSION}-py3-none-any.whl").read_bytes()
    (dist / f"{NAME}-1.17.0-py3-none-any.whl").write_bytes(built)

    assert _check(dist, source) == 1
    assert "exactly one wheel" in capsys.readouterr().out


def test_the_gate_fails_when_nothing_was_built(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A missing or empty dist directory must not read as success."""
    source = _source_root(tmp_path, SOURCE_MODULES)

    assert _check(tmp_path / "no-dist", source) == 1
    assert "not found" in capsys.readouterr().out

    empty = tmp_path / "dist"
    empty.mkdir()
    assert _check(empty, source) == 1
    assert "exactly one wheel" in capsys.readouterr().out
