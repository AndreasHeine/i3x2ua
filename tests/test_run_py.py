"""`run.py` startup environment tests (M21)."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import run


@pytest.fixture
def isolated_environ(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    """Swap `os.environ` for a plain dict so tests see exactly the keys run.py sets."""
    env: dict[str, str] = {}
    monkeypatch.setattr(os, "environ", env)
    yield env


def test_env_file_beats_windows_host_default(
    isolated_environ: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`.env` must be read before the Windows default, or `I3X_HOST=0.0.0.0` loses."""
    (tmp_path / ".env").write_text("I3X_HOST=0.0.0.0\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "name", "nt")
    run._prepare_environment()
    assert isolated_environ["I3X_HOST"] == "0.0.0.0"


def test_windows_default_applies_without_env(
    isolated_environ: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "name", "nt")
    run._prepare_environment()
    assert isolated_environ["I3X_HOST"] == "127.0.0.1"


def test_explicit_shell_value_beats_env_file(
    isolated_environ: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated_environ["I3X_HOST"] = "10.0.0.5"
    (tmp_path / ".env").write_text("I3X_HOST=0.0.0.0\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    run._prepare_environment()
    assert isolated_environ["I3X_HOST"] == "10.0.0.5"


def test_load_local_env_strips_inline_comments(isolated_environ: dict[str, str], tmp_path: Path) -> None:
    """Unquoted trailing comments are stripped; quoted `#` content stays intact."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "I3X_PORT=8000 # dev",
                'I3X_MESSAGE="hello # world"',
                'I3X_BROWSE_ROOTS=["nsu=urn:plant;i=1001"] # roots',
                "I3X_EMPTY=",
                "# full line comment",
                "NO_EQUALS_SIGN",
            ]
        ),
        encoding="utf-8",
    )
    run._load_local_env(str(env_file))
    assert isolated_environ["I3X_PORT"] == "8000"
    assert isolated_environ["I3X_MESSAGE"] == "hello # world"
    assert isolated_environ["I3X_BROWSE_ROOTS"] == '["nsu=urn:plant;i=1001"]'
    assert isolated_environ["I3X_EMPTY"] == ""
    assert "I3X_MESSAGE" in isolated_environ
    assert "NO_EQUALS_SIGN" not in isolated_environ


def test_load_local_env_keeps_unclosed_quote_verbatim(isolated_environ: dict[str, str], tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text('I3X_WEIRD="unclosed\n', encoding="utf-8")
    run._load_local_env(str(env_file))
    assert isolated_environ["I3X_WEIRD"] == '"unclosed'


def _command(**kwargs: Any) -> list[str]:
    defaults: dict[str, Any] = {"host": "127.0.0.1", "port": "8000", "reload_enabled": False, "uvicorn_loop": "none"}
    defaults.update(kwargs)
    return run._uvicorn_command(**defaults)


def test_uvicorn_command_applies_default_concurrency_limit(isolated_environ: dict[str, str]) -> None:
    assert _command()[-2:] == ["--limit-concurrency", "200"]


def test_uvicorn_command_honours_concurrency_limit_override(isolated_environ: dict[str, str]) -> None:
    isolated_environ["I3X_UVICORN_LIMIT_CONCURRENCY"] = "50"
    assert _command()[-2:] == ["--limit-concurrency", "50"]


def test_uvicorn_command_disables_concurrency_limit_with_zero(isolated_environ: dict[str, str]) -> None:
    isolated_environ["I3X_UVICORN_LIMIT_CONCURRENCY"] = "0"
    assert "--limit-concurrency" not in _command()


def test_uvicorn_command_inserts_reload_before_app_target(isolated_environ: dict[str, str]) -> None:
    command = _command(reload_enabled=True)
    assert command[:6] == [sys.executable, "-m", "uvicorn", "i3x_server.main:app", "--reload", "--host"]
