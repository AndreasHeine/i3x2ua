"""Third-party Actions run with the runner's token; pin them and scope what they can touch.

A `uses:` reference to a tag or major-range floats: the upstream repo can change what executes
between two runs, and the workflow has no way to notice. Renovate's `github-actions` manager keeps
SHA pins current, so pinning loses nothing and gains immutability. Permissions are the other half:
`id-token: write` mints an OIDC token that can reach external systems (Codecov), and granting it to
every job means every job - including the ones that only run lint - can exchange it. It belongs to
the one job that uploads coverage. Concurrency stops a push storm from queueing duplicate full
runs of the same workflow on the same ref.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = PROJECT_ROOT / ".github" / "workflows"
WORKFLOWS = sorted(WORKFLOW_DIR.glob("*.yml"))
QUALITY = (WORKFLOW_DIR / "quality.yml").read_text(encoding="utf-8")

USES_LINE = re.compile(r"^(\s*)uses:\s*([^\s#]+)(?:\s+#\s*(\S.*))?$")
PINNED_REF = re.compile(r"^[A-Za-z0-9./_-]+@[0-9a-f]{40}$")
_VERSION_COMMENT = re.compile(r"^v\d+(\.\d+)*$")


def _uses_refs(text: str) -> list[tuple[str, str]]:
    """The ``(ref, comment)`` pairs of every ``uses:`` line in a workflow."""
    refs: list[tuple[str, str]] = []
    for line in text.splitlines():
        match = USES_LINE.match(line)
        if match:
            refs.append((match.group(2), match.group(3) or ""))
    return refs


def _workflow_permissions(text: str) -> dict[str, str]:
    """The workflow-level ``permissions`` block, i.e. the indent-0 block's key/value pairs."""
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line == "permissions:"), None)
    if start is None:
        return {}
    granted: dict[str, str] = {}
    for line in lines[start + 1 :]:
        if not line.startswith("  "):
            break
        key, _, value = line.strip().partition(":")
        granted[key.strip()] = value.strip()
    return granted


def _job_block(text: str, job: str) -> str:
    """The YAML text of one job, i.e. up to the next job key at indent 2 (or the end of file)."""
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line == f"  {job}:"), None)
    assert start is not None, f"workflow must define a {job!r} job"
    end = next(
        (index for index in range(start + 1, len(lines)) if re.fullmatch(r"  [A-Za-z0-9_-]+:", lines[index])),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_every_action_reference_is_pinned_to_an_immutable_sha() -> None:
    """A tag or major range lets the upstream repo change what executes between two runs."""
    unpinned = [
        f"{path.name}: {line.strip()}"
        for path in WORKFLOWS
        for line in path.read_text(encoding="utf-8").splitlines()
        if (match := USES_LINE.match(line)) and not PINNED_REF.fullmatch(match.group(2))
    ]

    assert unpinned == [], f"actions must be pinned to 40-character SHAs: {unpinned}"


def test_every_pin_keeps_its_version_comment() -> None:
    """A bare SHA is unreadable; the comment is what reviewers and Renovate align on."""
    unlabelled = [
        f"{path.name}: {match.group(2)}"
        for path in WORKFLOWS
        for line in path.read_text(encoding="utf-8").splitlines()
        if (match := USES_LINE.match(line))
        and PINNED_REF.fullmatch(match.group(2))
        and not _VERSION_COMMENT.fullmatch(match.group(3) or "")
    ]

    assert unlabelled == [], f"every SHA pin needs a '# v...' comment: {unlabelled}"


def test_one_action_resolves_to_one_sha_across_all_workflows() -> None:
    """Different pins for the same action drift silently; the repo-wide mapping must be unique."""
    by_action: dict[str, set[str]] = {}
    for path in WORKFLOWS:
        for ref, _ in _uses_refs(path.read_text(encoding="utf-8")):
            action, _, sha = ref.partition("@")
            by_action.setdefault(action, set()).add(sha)

    drifted = {action: sorted(shas) for action, shas in by_action.items() if len(shas) > 1}
    assert drifted == {}, f"an action must pin to one SHA everywhere: {drifted}"


def test_no_workflow_grants_id_token_at_the_workflow_level() -> None:
    """id-token mints an OIDC token for external systems; jobs that never upload to them must not hold it."""
    granted = {
        path.name: _workflow_permissions(path.read_text(encoding="utf-8"))
        for path in WORKFLOWS
        if _workflow_permissions(path.read_text(encoding="utf-8"))
    }

    with_token = {name: permissions for name, permissions in granted.items() if "id-token" in permissions}
    assert with_token == {}, f"id-token must be scoped to a job, not the workflow: {sorted(with_token)}"


def test_only_the_coverage_job_can_mint_an_oidc_token() -> None:
    """The Codecov upload is the only consumer of the token, so only its job declares it."""
    pytest_block = _job_block(QUALITY, "pytest")

    assert "id-token: write" in pytest_block, "the pytest job needs the token Codecov exchanges"
    assert "use_oidc: true" in pytest_block, "the upload must actually use the OIDC token"
    assert QUALITY.count("id-token: write") == 1, "no other job may mint the token"

    # Every other job stays read-only for repository content.
    assert _workflow_permissions(QUALITY) == {"contents": "read"}


def test_the_quality_workflow_cancels_superseded_runs() -> None:
    """Without concurrency a push storm queues full duplicate runs of the same ref."""
    assert re.search(r"^concurrency:$", QUALITY, re.MULTILINE), "quality.yml must declare concurrency"
    assert re.search(r"^  group:", QUALITY, re.MULTILINE), "the group identifies workflow + ref"
    assert re.search(r"^  cancel-in-progress: true$", QUALITY, re.MULTILINE), (
        "a run superseded by a newer push must be cancelled, not queued"
    )
