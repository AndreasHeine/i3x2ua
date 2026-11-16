"""The images the gateway is built from must be immutable, and the toolchain too.

A tag is a pointer somebody else can move: `python:3.15.2-alpine` and `node:26-alpine` name whatever
upstream publishes next, so a rebuild - or an attacker with registry write access - changes what the
published image contains without changing a single line in this repository. A digest pin fixes the
bytes; Renovate's digest updates are then a proposed PR, not a silent move.

The `uv` install floats the same way (`pip install uv` resolves whatever is newest), so the pinned
version here is the version the developers and CI run, and a bump is a deliberate change.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
PROXY_DOCKERFILE = (PROJECT_ROOT / "nginx" / "Dockerfile").read_text(encoding="utf-8")
RENOVATE = json.loads((PROJECT_ROOT / ".github" / "renovate.json").read_text(encoding="utf-8"))

FROM_LINE = re.compile(r"^FROM (\S+)(?: AS (\S+))?$", re.MULTILINE)
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
UV_INSTALL = re.compile(r"pip install uv==(\d+\.\d+\.\d+)$", re.MULTILINE)


def _stages(text: str) -> list[tuple[str, str | None]]:
    """Every ``FROM`` line of a Dockerfile as ``(reference, stage name)``."""
    return [(match.group(1), match.group(2)) for match in FROM_LINE.finditer(text)]


def _split(reference: str) -> tuple[str, str | None]:
    """The ``image:tag`` part of a reference and the digest it pins, if it carries one."""
    image_tag, _, digest = reference.partition("@")
    return image_tag, digest or None


def _pull_of(name: str, text: str, stage: str) -> str:
    """The full reference the named stage pulls, so image, tag, and digest all compare."""
    reference = next((ref for ref, found in _stages(text) if found == stage), None)
    assert reference is not None, f"{name} must keep a stage named {stage!r}"

    image_tag, digest = _split(reference)
    assert digest is not None, f"{name}: stage {stage} pulls {image_tag} by tag, which upstream can move"
    return reference


def test_every_base_image_is_pinned_by_digest() -> None:
    """An unpinned FROM is a supply-chain hole: the tag moves without the repository changing."""
    for name, text in (("Dockerfile", DOCKERFILE), ("nginx/Dockerfile", PROXY_DOCKERFILE)):
        assert _stages(text), f"{name} must declare its base images"
        for reference, _stage in _stages(text):
            image_tag, digest = _split(reference)
            assert digest is not None, f"{name} pulls {image_tag} by tag, which upstream can move"
            assert DIGEST.match(digest), f"{name} carries a malformed digest for {image_tag}: {digest}"


def test_the_uv_and_image_digests_are_kept_current_by_the_dependency_bot() -> None:
    """A pinned digest is only safe if something proposes a newer one; a stale pin is a stale image.

    ``config:recommended`` defaults to ``pinDigests: false``, so a digest-only update is never
    proposed unless the configuration opts in for the managers that read these files.
    """
    assert RENOVATE["enabledManagers"] == ["pep621", "dockerfile", "docker-compose", "github-actions", "npm"]

    if RENOVATE.get("pinDigests") is True:
        return
    broad = [
        rule
        for rule in RENOVATE.get("packageRules", [])
        if rule.get("pinDigests") is True and not rule.get("matchUpdateTypes")
    ]
    assert broad, (
        "renovate.json must pin digests for image references without restricting the rule to major "
        "updates, otherwise a digest bump is never proposed and every pin goes stale"
    )

    # Compose references stay tag-pinned on purpose: docker-compose.yml builds the local image, and
    # docs/examples pull the published gateway's floating master tag, which a digest would freeze.
    for rule in broad:
        assert rule.get("matchManagers") == ["dockerfile"], (
            "digest pinning must cover the Dockerfiles only, not compose files that reference the "
            "published image by a floating tag"
        )


def test_the_builder_and_runtime_stages_pull_the_same_python_image() -> None:
    """The runtime copies the venv out of the builder; a different interpreter would ship mismatched."""
    builder = _pull_of("Dockerfile", DOCKERFILE, "builder")
    runtime = _pull_of("Dockerfile", DOCKERFILE, "runtime")

    assert builder == runtime, "builder and runtime must build and run on the same interpreter image"


def test_both_frontend_stages_pull_the_same_node_image() -> None:
    """The gateway image and the proxy image compile the same SPA; a different Node would produce
    different output from the same source."""
    gateway = _pull_of("Dockerfile", DOCKERFILE, "frontend")
    proxy = _pull_of("nginx/Dockerfile", PROXY_DOCKERFILE, "frontend")

    assert gateway == proxy, "both frontend stages must build on the same Node image"


def test_the_uv_installation_is_pinned_exactly() -> None:
    """``pip install uv`` resolves whatever is newest, so the build toolchain drifts with PyPI."""
    assert "pip install uv " not in DOCKERFILE, "uv must not be installed unpinned"

    pinned = UV_INSTALL.search(DOCKERFILE)
    assert pinned is not None, "the Dockerfile must pin uv to one exact version"
    assert pinned.group(1).count(".") == 2, f"uv pin {pinned.group(1)!r} is not an exact version"
