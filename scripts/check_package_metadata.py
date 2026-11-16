#!/usr/bin/env python3
"""Gate a ``uv build`` output against ``pyproject.toml``.

CI builds with ``uv build`` (``.github/workflows/quality.yml``) and calls this script so the declared
build backend is exercised instead of merely declared: the published image copies raw source rather
than a built distribution, so a packaging mistake - a dropped subpackage, a drifted
``requires-python``, a license that stops shipping - would otherwise only be noticed by whoever
installs the published package. The artifacts are compared with ``pyproject.toml`` *and* with the
repository's own source tree, so a distribution cannot disagree with the repository it came from.

The comparison is deliberately order-insensitive: the backend sorts specifier clauses
(``>=3.14,<3.16`` is written as ``<3.16,>=3.14``), so requirements are compared clause-wise.

Usage::

    python scripts/check_package_metadata.py [--dist dist]

Exit codes: 0 = the artifacts match the declaration, 1 = nothing was built or a check failed.
"""

from __future__ import annotations

import argparse
import re
import sys
import tarfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PYPROJECT = PROJECT_ROOT / "pyproject.toml"

# The gateway ships no compiled code, so a platform-specific tag would mean something was built wrong.
PURE_WHEEL_TAG = "py3-none-any"

_HEADER_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_SPEC_SEPARATORS = ("<", ">", "=", "!", "~")
_EXTRA_MARKER = re.compile(r'extra == "([^"]+)"')

_Clauses = tuple[str, ...]
_Core = dict[str, _Clauses]
_Extras = dict[str, dict[str, _Clauses]]


@dataclass(frozen=True)
class _Expectations:
    """What ``pyproject.toml`` declares a distribution must carry."""

    name: str
    version: str
    summary: str
    requires_python: _Clauses
    classifiers: frozenset[str]
    dependencies: _Core
    extras: _Extras
    packages: tuple[str, ...]


def _specifiers(raw: str) -> _Clauses:
    return tuple(sorted(clause.strip() for clause in raw.split(",")))


def _requirement(raw: str) -> tuple[str, _Clauses]:
    """A requirement as ``(name with extras, sorted specifier clauses)``; markers are dropped.

    The split point is the earliest specifier character, not the first separator tried:
    ``uvicorn>=0.54,<0.55`` must read as ``uvicorn`` with two clauses, not as ``uvicorn>=0.54,``.
    """
    value = raw.split(";", 1)[0].strip()
    positions = (position for separator in _SPEC_SEPARATORS if (position := value.find(separator)) != -1)
    index = min(positions, default=-1)
    if index == -1:
        return value, ()
    return value[:index], tuple(sorted(clause.strip() for clause in value[index:].split(",")))


def _requirements_from(values: list[str]) -> _Core:
    return {name: clauses for name, clauses in map(_requirement, values)}


def _headers(text: str) -> dict[str, list[str]]:
    """Email-style headers of a ``METADATA``/``PKG-INFO`` block, keeping repeated keys.

    Indented lines are continuation lines of a multi-line value - the license text is one - and only
    the first line of a value is kept.
    """
    values: dict[str, list[str]] = {}
    for line in text.splitlines():
        if not line or line[:1] in (" ", "\t"):
            continue
        key, _, value = line.partition(":")
        if value.strip() and _HEADER_KEY.fullmatch(key):
            values.setdefault(key, []).append(value.strip())
    return values


def _split_headers(headers: dict[str, list[str]]) -> tuple[_Core, _Extras]:
    """The core requirements and the per-extra requirements of a metadata block."""
    core: _Core = {}
    extras: _Extras = {}
    for raw in headers.get("Requires-Dist", []):
        name, clauses = _requirement(raw)
        marker = _EXTRA_MARKER.search(raw.split(";", 1)[1] if ";" in raw else "")
        if marker is None:
            core[name] = clauses
        else:
            extras.setdefault(marker.group(1), {})[name] = clauses
    return core, extras


def _declared_view(headers: dict[str, list[str]]) -> dict[str, object]:
    """The metadata fields the gate cares about, normalized so wheel and sdist can be compared."""
    core, extras = _split_headers(headers)
    return {
        "name": headers.get("Name", []),
        "version": headers.get("Version", []),
        "summary": headers.get("Summary", []),
        "requires-python": [_specifiers(value) for value in headers.get("Requires-Python", [])],
        "classifiers": sorted(headers.get("Classifier", [])),
        "license-file": sorted(headers.get("License-File", [])),
        "dependencies": core,
        "extras": extras,
    }


def _expectations(pyproject_path: Path) -> _Expectations:
    with pyproject_path.open("rb") as handle:
        data: dict[str, Any] = tomllib.load(handle)
    project: dict[str, Any] = data["project"]
    include = data.get("tool", {}).get("setuptools", {}).get("packages", {}).get("find", {}).get("include") or []
    optional: dict[str, Any] = project.get("optional-dependencies", {})
    return _Expectations(
        name=str(project["name"]),
        version=str(project["version"]),
        summary=str(project["description"]),
        requires_python=_specifiers(str(project["requires-python"])),
        classifiers=frozenset(str(classifier) for classifier in project.get("classifiers", [])),
        dependencies=_requirements_from([str(dependency) for dependency in project["dependencies"]]),
        extras={
            extra: _requirements_from([str(item) for item in requirements]) for extra, requirements in optional.items()
        },
        # ``i3x_server*`` is what the build configuration declares as packaged content.
        packages=tuple(sorted({str(pattern).rstrip("*") for pattern in include})),
    )


def _read_wheel(wheel: Path) -> tuple[dict[str, list[str]], dict[str, bytes], str, str, str, str] | None:
    """The wheel's headers, its contents keyed by archive path, and the dist-info name/version/tag."""
    try:
        with zipfile.ZipFile(wheel) as archive:
            payload = {name: archive.read(name) for name in archive.namelist()}
    except (zipfile.BadZipFile, OSError) as exc:
        print(f"Package metadata check failed: {wheel.name} is not a readable wheel: {exc}")
        return None

    dist_infos = {name.split("/")[0] for name in payload if name.split("/")[0].endswith(".dist-info")}
    if len(dist_infos) != 1:
        print(f"Package metadata check failed: {wheel.name} carries no single dist-info directory.")
        return None
    dist_info = next(iter(dist_infos))
    metadata_name, _, metadata_version = dist_info[: -len(".dist-info")].rpartition("-")

    metadata = payload.get(f"{dist_info}/METADATA")
    wheel_file = payload.get(f"{dist_info}/WHEEL")
    if metadata is None or wheel_file is None:
        print(f"Package metadata check failed: {wheel.name} carries no METADATA or WHEEL.")
        return None
    tag = _headers(wheel_file.decode("utf-8", "replace")).get("Tag", [""])[0]
    return _headers(metadata.decode("utf-8", "replace")), payload, dist_info, metadata_name, metadata_version, tag


def _read_sdist(sdist: Path) -> tuple[dict[str, list[str]], set[str], str, bytes | None] | None:
    """The sdist's headers, members, root directory, and the license text it carries."""
    try:
        with tarfile.open(sdist, "r:gz") as archive:
            members = {member.name for member in archive.getmembers() if member.isfile()}
            # The top-level PKG-INFO is the distribution's metadata; the deeper egg-info copy holds
            # what setuptools writes there, which is not what a reader of the sdist sees.
            pkg_infos = [member for member in members if member.endswith("/PKG-INFO") and member.count("/") == 1]
            if not pkg_infos:
                print(f"Package metadata check failed: {sdist.name} carries no PKG-INFO.")
                return None
            pkg_info = min(pkg_infos, key=len)
            root = pkg_info[: -len("/PKG-INFO")]
            handle = archive.extractfile(pkg_info)
            if handle is None:
                print(f"Package metadata check failed: {sdist.name} PKG-INFO could not be read.")
                return None
            headers = _headers(bytes(handle.read()).decode("utf-8", "replace"))
            # The license is optional here: whether it must be present is a gate decision, not a
            # reading one, and ``extractfile`` raises for a member the archive does not hold.
            license_member = f"{root}/LICENSE"
            license_handle = archive.extractfile(license_member) if license_member in members else None
            shipped_license = None if license_handle is None else bytes(license_handle.read())
    except (tarfile.TarError, OSError) as exc:
        print(f"Package metadata check failed: {sdist.name} is not a readable sdist: {exc}")
        return None

    return headers, members, root, shipped_license


def _check_metadata(label: str, headers: dict[str, list[str]], expected: _Expectations, failures: list[str]) -> None:
    """Every declared field a metadata block must carry, compared clause-wise."""
    core, extras = _split_headers(headers)
    name = headers.get("Name", [""])[0]
    version = headers.get("Version", [""])[0]
    summary = headers.get("Summary", [""])[0]
    requires_python = _specifiers(headers.get("Requires-Python", [""])[0])
    classifiers = frozenset(headers.get("Classifier", []))

    if name != expected.name:
        failures.append(f"{label} metadata name {name!r} != declared {expected.name!r}")
    if version != expected.version:
        failures.append(f"{label} metadata version {version!r} != declared {expected.version!r}")
    if summary != expected.summary:
        failures.append(f"{label} metadata summary {summary!r} != declared {expected.summary!r}")
    if requires_python != expected.requires_python:
        failures.append(f"{label} metadata requires-python {requires_python!r} != declared specifiers")

    missing_classifiers = sorted(expected.classifiers - classifiers)
    if missing_classifiers:
        failures.append(f"{label} metadata lacks the license classifier(s) declared: {missing_classifiers}")
    if not headers.get("License-File"):
        failures.append(f"{label} metadata declares no license file (License-File)")
    if core != expected.dependencies:
        failures.append(f"{label} metadata dependencies differ: {_drift(core, expected.dependencies)}")
    if extras != expected.extras:
        failures.append(f"{label} metadata extras differ: {_drift(extras, expected.extras)}")


def _drift(actual: Mapping[str, Any], declared: Mapping[str, Any]) -> list[str]:
    """Only the entries that disagree, so a failure names the drift instead of the whole table."""
    return sorted(
        f"{key}: {actual.get(key)!r} != {declared.get(key)!r}"
        for key in set(actual) | set(declared)
        if actual.get(key) != declared.get(key)
    )


def _source_modules(source_root: Path, packages: tuple[str, ...]) -> set[str]:
    """Every Python module the repository declares as packaged content."""
    modules: set[str] = set()
    for package in packages:
        for pattern in (f"{package}/*.py", f"{package}/**/*.py"):
            modules.update(path.relative_to(source_root).as_posix() for path in source_root.glob(pattern))
    return modules


def _top_level_paths(names: list[str]) -> set[str]:
    """Every top-level entry of a wheel archive, including a stray file at the archive root."""
    return {name.split("/")[0] for name in names}


def _check_wheel(
    wheel: Path,
    payload: dict[str, bytes],
    dist_info: str,
    metadata_name: str,
    metadata_version: str,
    wheel_tag: str,
    expected: _Expectations,
    license_text: bytes,
    source_modules: set[str],
    failures: list[str],
) -> None:
    """The wheel must carry the declared metadata, the packaged modules, and the license text."""
    if wheel_tag != PURE_WHEEL_TAG:
        failures.append(f"wheel tag {wheel_tag!r} is not {PURE_WHEEL_TAG!r}; the gateway ships a pure-python wheel")
    if wheel.name != f"{metadata_name}-{metadata_version}-{wheel_tag}.whl":
        failures.append(f"wheel filename {wheel.name!r} does not carry the name and version it declares")
    if metadata_name != expected.name:
        failures.append(f"wheel dist-info name {metadata_name!r} != declared {expected.name!r}")
    if metadata_version != expected.version:
        failures.append(f"wheel dist-info version {metadata_version!r} != declared {expected.version!r}")

    top_level_bytes = payload.get(f"{dist_info}/top_level.txt")
    if top_level_bytes is None:
        failures.append("wheel carries no top_level.txt")
    else:
        top_level = sorted(line for line in top_level_bytes.decode("utf-8", "replace").splitlines() if line)
        if tuple(top_level) != expected.packages:
            failures.append(f"wheel top-level packages {top_level!r} != declared {expected.packages!r}")

    packaged = sorted(entry for entry in _top_level_paths(list(payload)) if not entry.endswith(".dist-info"))
    leaked = sorted(entry for entry in packaged if entry not in expected.packages)
    if leaked:
        failures.append(f"wheel carries files outside the packaged top-level packages: {leaked}")

    wheel_modules = {name for name in payload if name.endswith(".py") and name.split("/")[0] in expected.packages}
    missing = sorted(source_modules - wheel_modules)
    if missing:
        failures.append(f"modules missing from the wheel: {missing}")
    unexpected = sorted(wheel_modules - source_modules)
    if unexpected:
        failures.append(f"wheel modules that are not in the source tree: {unexpected}")

    licenses = sorted(name for name in payload if name.endswith(".dist-info/licenses/LICENSE"))
    if not licenses:
        failures.append("wheel does not ship the repository's LICENSE file")
    elif payload[licenses[0]] != license_text:
        failures.append("wheel LICENSE differs from the repository's LICENSE file")


def _check_sdist(
    sdist: Path,
    members: set[str],
    root: str,
    wheel_version: str,
    expected: _Expectations,
    expected_license: bytes,
    shipped_license: bytes | None,
    source_modules: set[str],
    failures: list[str],
) -> None:
    """The sdist must carry the declared metadata, the packaged modules, and the license text."""
    root_name, _, root_version = root.rpartition("-")
    if sdist.name != f"{root}.tar.gz":
        failures.append(f"sdist filename {sdist.name!r} does not match its root directory {root!r}")
    if root_name != expected.name:
        failures.append(f"sdist root name {root_name!r} != declared {expected.name!r}")
    if root_version != wheel_version:
        failures.append(f"sdist version {root_version!r} disagrees with the wheel version {wheel_version!r}")
    if f"{root}/pyproject.toml" not in members:
        failures.append("sdist lacks pyproject.toml")

    # The AGPL text must travel with the source distribution, and every packaged module must be there
    # so a reader can rebuild it. Tests and egg-info may appear; packaged content may not be absent.
    if shipped_license is None:
        failures.append("sdist does not ship the repository's LICENSE file")
    elif shipped_license != expected_license:
        failures.append("sdist LICENSE differs from the repository's LICENSE file")

    prefix = f"{root}/"
    sdist_modules = {
        member.removeprefix(prefix) for member in members if member.startswith(prefix) and member.endswith(".py")
    }
    missing = sorted(source_modules - sdist_modules)
    if missing:
        failures.append(f"modules missing from the sdist: {missing}")


def _report(dist: Path, wheels: list[Path], sdists: list[Path], failures: list[str]) -> None:
    print(f"Package metadata check: {dist}")
    for wheel in wheels:
        print(f"  wheel: {wheel.name}")
    for sdist in sdists:
        print(f"  sdist: {sdist.name}")
    if failures:
        print("Metadata gates failed: " + "; ".join(failures))
        return
    print("Package metadata gates passed.")


def check_package(dist: Path, *, pyproject: Path = DEFAULT_PYPROJECT, source_root: Path = PROJECT_ROOT) -> int:
    if not dist.is_dir():
        print(f"Package metadata check failed: {dist} not found. Run uv build first.")
        return 1

    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    failures: list[str] = []
    if len(wheels) != 1:
        failures.append(f"dist/ must contain exactly one wheel (found {len(wheels)})")
    if len(sdists) != 1:
        failures.append(f"dist/ must contain exactly one sdist (found {len(sdists)})")
    if failures:
        _report(dist, wheels, sdists, failures)
        return 1

    wheel, sdist = wheels[0], sdists[0]
    expected = _expectations(pyproject)
    source_modules = _source_modules(source_root, expected.packages)
    try:
        license_text = (source_root / "LICENSE").read_bytes()
    except OSError as exc:
        print(f"Package metadata check failed: {source_root / 'LICENSE'} could not be read: {exc}")
        return 1

    wheel_read = _read_wheel(wheel)
    sdist_read = _read_sdist(sdist)
    if wheel_read is None or sdist_read is None:
        _report(dist, wheels, sdists, ["wheel and sdist metadata could not be read"])
        return 1

    wheel_headers, wheel_payload, dist_info, wheel_name, wheel_version, wheel_tag = wheel_read
    sdist_headers, sdist_members, sdist_root, shipped_license = sdist_read

    _check_metadata("wheel", wheel_headers, expected, failures)
    _check_metadata("sdist", sdist_headers, expected, failures)

    wheel_view = _declared_view(wheel_headers)
    sdist_view = _declared_view(sdist_headers)
    differing = sorted(key for key in wheel_view if wheel_view[key] != sdist_view[key])
    if differing:
        failures.append(f"wheel and sdist metadata disagree on: {differing}")

    _check_wheel(
        wheel,
        wheel_payload,
        dist_info,
        wheel_name,
        wheel_version,
        wheel_tag,
        expected,
        license_text,
        source_modules,
        failures,
    )
    _check_sdist(
        sdist,
        sdist_members,
        sdist_root,
        wheel_version,
        expected,
        license_text,
        shipped_license,
        source_modules,
        failures,
    )

    _report(dist, wheels, sdists, failures)
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", default="dist", help="directory holding the built wheel and sdist")
    parser.add_argument("--pyproject", default=str(DEFAULT_PYPROJECT), help="pyproject.toml to compare with")
    parser.add_argument("--source-root", default=str(PROJECT_ROOT), help="repository tree the wheel must match")
    args = parser.parse_args()
    return check_package(Path(args.dist), pyproject=Path(args.pyproject), source_root=Path(args.source_root))


if __name__ == "__main__":
    sys.exit(main())
