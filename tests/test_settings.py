from __future__ import annotations

import pytest
from pydantic import ValidationError
from pytest import MonkeyPatch

from i3x_server.config.settings import Settings


def test_settings_trim_whitespace_from_env_values(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("I3X_MODEL_PRELOAD_ON_STARTUP", " true ")
    monkeypatch.setenv("I3X_MODEL_PRELOAD_BLOCKING", " false ")
    monkeypatch.setenv("I3X_SUBSCRIPTIONS_INITIAL_VALUES", " 1 ")

    settings = Settings()

    assert settings.model_preload_on_startup is True
    assert settings.model_preload_blocking is False
    assert settings.subscriptions_initial_values is True


def test_settings_can_disable_opcua_schema_fields(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("I3X_MCP_INCLUDE_OPCUA_METADATA", "false")

    settings = Settings()

    assert settings.mcp_include_opcua_metadata is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("nsu=urn:plant;s=Machine1", ["nsu=urn:plant;s=Machine1"]),
        (
            '["nsu=urn:plant;s=Machine1","nsu=urn:plant;s=Machine2","nsu=urn:plant;s=Machine1"]',
            ["nsu=urn:plant;s=Machine1", "nsu=urn:plant;s=Machine2"],
        ),
        ("nsu=urn:plant;s=Machine;part=1,2", ["nsu=urn:plant;s=Machine;part=1,2"]),
        ("nsu=urn:plant;i=4294967295", ["nsu=urn:plant;i=4294967295"]),
        (
            "nsu=urn:plant;g=12345678-1234-1234-1234-123456789abc",
            ["nsu=urn:plant;g=12345678-1234-1234-1234-123456789abc"],
        ),
        ("nsu=urn:plant;b=AQID", ["nsu=urn:plant;b=AQID"]),
    ],
)
def test_settings_browse_roots(monkeypatch: MonkeyPatch, raw: str, expected: list[str]) -> None:
    monkeypatch.setenv("I3X_OPCUA_BROWSE_ROOTS", raw)
    assert Settings().opcua_browse_roots == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " ",
        "[]",
        "[",
        "[1]",
        '[""]',
        "null",
        "ns=2;s=Machine1",
        "nsu=;s=Machine1",
        "nsu=urn:plant;s=",
        "svr=1;nsu=urn:plant;i=42",
        "srv=1;nsu=urn:plant;i=42",
        "nsu=urn:plant;unknown=1;i=42",
        "nsu=urn:plant;i=-1",
        "nsu=urn:plant;i=4294967296",
        "nsu=urn:plant;i=1;unknown=1",
        "nsu=urn:plant;g=bad",
        "nsu=urn:plant;b=!!!",
        '["nsu=urn:plant;s=Machine\\n1"]',
    ],
)
def test_settings_reject_invalid_browse_roots(monkeypatch: MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("I3X_OPCUA_BROWSE_ROOTS", raw)
    with pytest.raises(ValidationError):
        Settings()


def test_settings_default_browse_roots(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("I3X_OPCUA_BROWSE_ROOTS", raising=False)
    assert Settings(_env_file=None).opcua_browse_roots is None  # type: ignore[call-arg]
