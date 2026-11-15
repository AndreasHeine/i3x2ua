"""Private key material must be generated at setup, never committed.

The repository tracked development sample keys - ``certs/https-sample/privkey.pem`` and
``certs/opcua-client-sample/client-key.pem`` - while compose and ``.env-example`` pointed at them.
In a public repository that means the keys are published: an OPC UA client certificate whose key
anyone can read carries no identity, and a TLS key everyone can read lets an impostor terminate
the edge. Generation scripts already exist (``scripts/generate_*_cert.py``), so the sample
material is generated at setup instead, and the previously committed keys must be treated as
withdrawn by anything that trusted them.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GITIGNORE = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
SECURITY = (PROJECT_ROOT / "SECURITY.md").read_text(encoding="utf-8")
HTTPS_SAMPLE_README = (PROJECT_ROOT / "certs" / "https-sample" / "README.md").read_text(encoding="utf-8")
OPCUA_SAMPLE_README = (PROJECT_ROOT / "certs" / "opcua-client-sample" / "README.md").read_text(encoding="utf-8")

# A PEM private key block. The pieces are concatenated so this file cannot match itself.
_PEM_PRIVATE_KEY = re.compile("-----BEGIN [A-Z0-9 ]*" + "PRIVATE KEY-----")


def _tracked_files() -> list[str]:
    """Every tracked path, so a re-added key cannot hide behind a worktree-only file."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=True,
    )
    return [entry.decode("utf-8", errors="replace") for entry in result.stdout.split(b"\0") if entry]


def _tracked_paths_matching(pattern: str) -> list[str]:
    return [path for path in _tracked_files() if Path(path).match(pattern)]


def test_no_tracked_file_contains_private_key_material() -> None:
    """A committed private key is readable by anyone with the repository."""
    offenders: list[str] = []
    for path in _tracked_files():
        try:
            text = (PROJECT_ROOT / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary artefacts are not key material in PEM form
        if _PEM_PRIVATE_KEY.search(text) is not None:
            offenders.append(path)

    assert offenders == [], f"private key material is tracked: {offenders}"


@pytest.mark.parametrize("pattern", ["certs/**/*.pem", "**/*.pem", "**/*.key"])
def test_no_certificate_or_key_files_are_tracked(pattern: str) -> None:
    """Sample bundles are generated at setup, so no PEM material is in the repository."""
    assert _tracked_paths_matching(pattern) == []


def test_regenerated_material_stays_out_of_git() -> None:
    """Generating the sample bundles locally must not make them committable."""
    assert "certs/**/*.pem" in GITIGNORE


def test_setup_generation_is_documented_for_both_bundles() -> None:
    """The generator scripts are the only way to produce the sample material."""
    assert (PROJECT_ROOT / "scripts" / "generate_https_dev_cert.py").exists()
    assert (PROJECT_ROOT / "scripts" / "generate_opcua_client_cert.py").exists()

    assert "generate_https_dev_cert.py" in HTTPS_SAMPLE_README
    assert "generate_opcua_client_cert.py" in OPCUA_SAMPLE_README
    for readme in (HTTPS_SAMPLE_README, OPCUA_SAMPLE_README):
        assert "not tracked" in readme or "never committed" in readme


def test_withdrawn_sample_keys_must_not_be_trusted() -> None:
    """Anything that trusted the committed keys has to be told they are no longer valid."""
    assert "withdrawn" in SECURITY.lower()
    assert "not trusted" in SECURITY.lower() or "must not be trusted" in SECURITY.lower()
