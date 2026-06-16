from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from i3x_server.mcp import load_overrides


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_load_overrides_applies_valid_payload(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    overrides_path = tmp_path / "mcp_overrides.json"

    schema = {
        "type": "object",
        "properties": {
            "tools": {"type": "object"},
            "prompts": {"type": "object"},
        },
        "required": ["tools", "prompts"],
        "additionalProperties": False,
    }
    overrides = {
        "tools": {
            "getNamespaces": {
                "description": "Prefer this for namespace discovery",
                "priority": "high",
                "keywords": ["namespaces"],
            }
        },
        "prompts": {},
    }

    _write_json(schema_path, schema)
    _write_json(overrides_path, overrides)

    loaded = load_overrides(path=overrides_path, schema_path=schema_path)

    assert loaded == overrides


def test_load_overrides_skips_invalid_payload_with_clear_log(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    schema_path = tmp_path / "schema.json"
    overrides_path = tmp_path / "mcp_overrides.json"

    schema = {
        "type": "object",
        "properties": {
            "tools": {"type": "object"},
            "prompts": {"type": "object"},
        },
        "required": ["tools", "prompts"],
        "additionalProperties": False,
    }
    invalid_overrides = {
        "tools": {
            "getNamespaces": {
                "description": "Still valid at this level",
            }
        }
    }

    _write_json(schema_path, schema)
    _write_json(overrides_path, invalid_overrides)

    caplog.set_level(logging.WARNING)
    loaded = load_overrides(path=overrides_path, schema_path=schema_path)

    assert loaded == {}
    assert "validation failed" in caplog.text
    assert "Overrides were not applied" in caplog.text


def test_load_overrides_skips_malformed_json_with_clear_log(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    schema_path = tmp_path / "schema.json"
    overrides_path = tmp_path / "mcp_overrides.json"

    _write_json(schema_path, {"type": "object"})
    overrides_path.write_text("{this is not valid json", encoding="utf-8")

    caplog.set_level(logging.WARNING)
    loaded = load_overrides(path=overrides_path, schema_path=schema_path)

    assert loaded == {}
    assert "Invalid JSON in MCP overrides" in caplog.text
    assert "Skipping MCP overrides" in caplog.text
