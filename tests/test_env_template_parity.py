"""Template/compose ↔ ``Settings`` parity (env surface drift).

``Settings`` uses ``extra="ignore"``, so a renamed or misspelled ``I3X_*`` key in the deployment
templates is silently invisible: the operator believes a knob is set while the code runs its
default. These tests bind the templates to the field surface instead:

- every ``I3X_*`` settings field must be mentioned in ``.env-example``
- every ``I3X_*`` assignment in the templates must be a real settings field or a runtime/deployment
  key that the launcher (``run.py``) or the compose stack actually reads
- every ``.env-example`` assignment must parse to the field's default value: the template documents
  defaults, deployment tuning belongs in ``docker-compose.yml``
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from i3x_server.config.settings import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = (PROJECT_ROOT / ".env-example").read_text(encoding="utf-8")
COMPOSE = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
RUN_PY = (PROJECT_ROOT / "run.py").read_text(encoding="utf-8")

# Keys ``Settings`` does not own: the uvicorn launcher reads them directly, and the certificate
# directory / proxy allow-list are consumed by the compose stack and nginx rather than by the app.
LAUNCHER_KEYS = {
    "I3X_HOST",
    "I3X_PORT",
    "I3X_RELOAD",
    "I3X_UVICORN_LOOP",
    "I3X_UVICORN_LIMIT_CONCURRENCY",
    "I3X_UVICORN_GRACEFUL_SHUTDOWN_SECONDS",
}
DEPLOYMENT_KEYS = {"I3X_OPCUA_CERTS_DIR", "I3X_FORWARDED_ALLOW_IPS"}

_SETTINGS_ENV_NAMES: dict[str, str] = {f"I3X_{name.upper()}": name for name in Settings.model_fields}


def _env_assignments(text: str) -> dict[str, str]:
    """The ``KEY=value`` assignments of an env template, comments dropped."""
    assignments: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        assignments[name.strip()] = value.strip()
    return assignments


def _env_comment_block(text: str, name: str) -> str:
    """The comment lines directly above the assignment of ``name``."""
    lines = text.splitlines()
    index = next(index for index, line in enumerate(lines) if line.startswith(f"{name}="))
    comments: list[str] = []
    for line in reversed(lines[:index]):
        if line.startswith("#"):
            comments.append(line)
        elif line.strip():
            break
    return "\n".join(reversed(comments))


def _compose_service_block(service: str) -> str:
    """The YAML text of one compose service, i.e. up to the next service key at indent 2."""
    lines = COMPOSE.splitlines()
    start = next(index for index, line in enumerate(lines) if line == f"  {service}:")
    end = next(index for index in range(start + 1, len(lines)) if re.fullmatch(r"  [A-Za-z0-9_-]+:", lines[index]))
    return "\n".join(lines[start:end])


def _compose_environment(service: str) -> dict[str, str]:
    """The ``KEY: value`` entries of a compose service's ``environment`` mapping."""
    block = _compose_service_block(service)
    lines = block.splitlines()
    start = next(index for index, line in enumerate(lines) if line.strip() == "environment:")
    entries: dict[str, str] = {}
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith("      "):
            break  # the mapping ends at the next unindented section
        match = re.fullmatch(r" {6}([A-Z0-9_]+):\s*(.*?)\s*", line)
        if match:
            value = match.group(2).split("#", 1)[0].strip().strip("\"'")
            entries[match.group(1)] = value
    return entries


def _parse_as_field(field_name: str, raw: str) -> Any:
    """Parse a template value the way ``Settings`` would, running the field validators.

    A ``[...]`` value is decoded as JSON because the list-typed settings fields expect
    ``NoDecode``-free JSON arrays in the environment.
    """
    value: Any = json.loads(raw) if raw.startswith("[") else raw
    return Settings(**{field_name: value}, _env_file=None).model_dump()[field_name]  # type: ignore[call-arg]


def _default_of(field_name: str) -> Any:
    return Settings(_env_file=None).model_dump()[field_name]  # type: ignore[call-arg]


def test_every_settings_field_is_documented_in_the_env_template() -> None:
    """An undocumented field is a knob nobody can set (``extra="ignore"`` hides renames)."""
    missing = [name for name in _SETTINGS_ENV_NAMES if name not in ENV_EXAMPLE]
    assert missing == [], f".env-example must mention every settings env key; missing: {missing}"


def test_env_template_has_no_unknown_or_renamed_i3x_keys() -> None:
    """A misspelled ``I3X_*`` key would be ignored by pydantic-settings and silently keep defaults."""
    known = set(_SETTINGS_ENV_NAMES) | LAUNCHER_KEYS | DEPLOYMENT_KEYS
    unknown = [name for name in _env_assignments(ENV_EXAMPLE) if name.startswith("I3X_") and name not in known]
    assert unknown == [], f".env-example sets keys no settings field or launcher reads: {unknown}"


def test_compose_sets_only_known_i3x_keys() -> None:
    """Compose must not advertise a knob the application does not consume."""
    known = set(_SETTINGS_ENV_NAMES) | LAUNCHER_KEYS | DEPLOYMENT_KEYS
    unknown = [name for name in _compose_environment("i3x2ua") if name.startswith("I3X_") and name not in known]
    assert unknown == [], f"docker-compose.yml sets keys no settings field or launcher reads: {unknown}"


def test_template_and_compose_values_parse_to_their_declared_types() -> None:
    """Values must survive the field validators, otherwise the deployment fails at startup."""
    for text, source in ((ENV_EXAMPLE, ".env-example"), (_compose_environment("i3x2ua"), "docker-compose.yml")):
        assignments = text if isinstance(text, dict) else _env_assignments(text)
        for name, raw in assignments.items():
            field_name = _SETTINGS_ENV_NAMES.get(name)
            if field_name is None:
                continue
            try:
                _parse_as_field(field_name, raw)
            except Exception as exc:  # noqa: BLE001 - the failure message is the assertion
                pytest.fail(f"{source}: {name}={raw!r} is not a valid value for {field_name}: {exc}")


def test_env_template_documents_the_settings_defaults() -> None:
    """The template is the default documentation; deviations belong in the compose stack.

    An empty assignment stands for "unset", which matches a ``None``/empty default.
    """
    mismatches: list[str] = []
    for name, raw in _env_assignments(ENV_EXAMPLE).items():
        field_name = _SETTINGS_ENV_NAMES.get(name)
        if field_name is None:
            continue
        default = _default_of(field_name)
        if raw == "" and not default:
            continue
        parsed = _parse_as_field(field_name, raw)
        if parsed != default:
            mismatches.append(f"{name}={raw!r} (default {default!r})")
    assert mismatches == [], f".env-example must show the defaults: {mismatches}"


def test_launcher_keys_are_actually_read_by_run_py() -> None:
    """The whitelist is self-checking: a launcher key nobody reads is documentation drift."""
    for key in LAUNCHER_KEYS:
        assert key in RUN_PY, f"{key} is whitelisted as a launcher key but run.py never reads it"


def test_deployment_keys_are_actually_read_by_the_compose_stack() -> None:
    """Certificate directory and proxy allow-list are consumed by compose/nginx, not by Settings."""
    for key in DEPLOYMENT_KEYS:
        assert key in COMPOSE, f"{key} is whitelisted as a deployment key but docker-compose.yml never uses it"


def test_log_format_documented_in_the_env_template() -> None:
    """``I3X_LOG_FORMAT`` is supported by ``Settings``; the template must name both renderings."""
    assert "I3X_LOG_FORMAT=text" in ENV_EXAMPLE
    comment = _env_comment_block(ENV_EXAMPLE, "I3X_LOG_FORMAT")
    assert "json" in comment.lower(), "the template must document the json log format"
    assert "text" in comment.lower(), "the template must document the default text log format"
