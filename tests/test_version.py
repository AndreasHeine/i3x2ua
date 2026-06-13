from __future__ import annotations

from pathlib import Path

from pytest import MonkeyPatch

import i3x_server.version as version


def _clear_server_version_cache() -> None:
    version.get_server_version.cache_clear()


def test_server_version_defaults_to_master(monkeypatch: MonkeyPatch) -> None:
    _clear_server_version_cache()
    monkeypatch.setattr(version, "_SERVER_VERSION_FILE", Path("missing-version-file.txt"))

    assert version.get_server_version() == "master"


def test_server_version_uses_file_value(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    _clear_server_version_cache()
    version_file = tmp_path / "server-version.txt"
    version_file.write_text("1.1.0\n", encoding="utf-8")
    monkeypatch.setattr(version, "_SERVER_VERSION_FILE", version_file)

    assert version.get_server_version() == "1.1.0"


def test_server_version_empty_file_falls_back_to_master(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    _clear_server_version_cache()
    version_file = tmp_path / "server-version.txt"
    version_file.write_text("\n", encoding="utf-8")
    monkeypatch.setattr(version, "_SERVER_VERSION_FILE", version_file)

    assert version.get_server_version() == "master"
