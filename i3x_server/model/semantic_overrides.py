"""Configurable semantic profiles loaded from ``overrides/semantic_overrides.json``.

The built-in profiles (:mod:`i3x_server.model.semantic_profiles`) are the default. A shipped
overrides file extends them (new profile ids) or replaces a built-in entirely (existing id);
custom profiles keep descending priority, precede remaining built-ins on equal priority, and
keep their file order. Unlike MCP overrides, semantic configuration is part of the model
contract: a present-but-invalid or unreadable file aborts startup instead of silently serving
defaults, so a deployment never maps with profiles its operator did not configure.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from i3x_server.model.semantic_profiles import (
    ProfileOverride,
    SemanticProfile,
    load_default_profiles,
)

logger = logging.getLogger(__name__)

DEFAULT_SEMANTIC_OVERRIDES_PATH = Path("overrides/semantic_overrides.json")
DEFAULT_SEMANTIC_OVERRIDES_SCHEMA_PATH = Path("overrides/semantic_overrides.schema.json")


class SemanticOverridesError(RuntimeError):
    """Semantic overrides configuration is present but unusable; startup must fail with this."""


def _resolve_override_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return Path(__file__).resolve().parents[2] / candidate


def _read_json_mapping(path: Path, *, label: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as file:
            payload = json.load(file)
    except json.JSONDecodeError as exc:
        raise SemanticOverridesError(
            f"Invalid JSON in {label} at {path}:{exc.lineno}:{exc.colno} ({exc.msg})."
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise SemanticOverridesError(f"{label} could not be read at {path} ({type(exc).__name__}: {exc}).") from exc
    if not isinstance(payload, dict):
        raise SemanticOverridesError(f"{label} must contain a JSON object at {path}.")
    return payload


def _validate_schema(document: Mapping[str, Any], schema_path: Path, override_path: Path) -> None:
    try:
        from jsonschema import SchemaError, ValidationError, validate
    except ImportError as exc:
        raise SemanticOverridesError(
            "jsonschema dependency is missing; cannot validate semantic overrides at "
            f"{override_path}. Install the jsonschema dependency or remove the file."
        ) from exc

    if not schema_path.exists():
        raise SemanticOverridesError(
            f"Semantic overrides schema file not found at {schema_path}; "
            f"cannot validate {override_path}. Keep the schema file next to the overrides file."
        )
    schema = _read_json_mapping(schema_path, label="Semantic overrides schema")
    try:
        validate(instance=document, schema=schema)
    except ValidationError as exc:
        location = ".".join(str(part) for part in exc.absolute_path) or "<root>"
        raise SemanticOverridesError(
            f"Semantic overrides validation failed for {override_path} at {location}: {exc.message}"
        ) from exc
    except SchemaError as exc:
        raise SemanticOverridesError(f"Semantic overrides schema at {schema_path} is invalid ({exc.message}).") from exc


def _parse_profile(entry: Mapping[str, Any], index: int, override_path: Path) -> SemanticProfile:
    overrides: list[ProfileOverride] = []
    for override_index, raw_override in enumerate(entry.get("overrides", [])):
        try:
            overrides.append(
                ProfileOverride(
                    type_name=str(raw_override["type_name"]),
                    namespace_uri=raw_override.get("namespace_uri"),
                    semantic_role=raw_override.get("semantic_role"),
                    force_hierarchy=bool(raw_override.get("force_hierarchy", False)),
                    force_composition=bool(raw_override.get("force_composition", False)),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SemanticOverridesError(
                f"Semantic overrides at {override_path} profile {index} override {override_index} "
                f"is invalid ({type(exc).__name__}: {exc}). An override cannot set both "
                "force_hierarchy and force_composition; semantic_role must be one of the i3X roles."
            ) from exc
    return SemanticProfile(
        profile_id=str(entry["profile_id"]),
        priority=int(entry["priority"]),
        namespace_uri=str(entry["namespace_uri"]),
        known_types=tuple(str(item) for item in entry.get("known_types", [])),
        hierarchy_references=tuple(str(item) for item in entry.get("hierarchy_references", [])),
        composition_references=tuple(str(item) for item in entry.get("composition_references", [])),
        graph_references=tuple(str(item) for item in entry.get("graph_references", [])),
        overrides=tuple(overrides),
    )


def _merge_profiles(custom: tuple[SemanticProfile, ...]) -> tuple[SemanticProfile, ...]:
    """Merge custom profiles into the built-ins, preserving the documented precedence.

    A custom profile with a built-in id replaces that built-in entirely; new ids extend it.
    The combined tuple is ordered by descending priority: the stable sort keeps custom
    profiles ahead of the remaining built-ins on equal priority, and file order among
    customs, so the first matching profile wins exactly as built-in resolution does.
    """
    replaced_ids = {profile.profile_id for profile in custom}
    remaining_builtins = tuple(profile for profile in load_default_profiles() if profile.profile_id not in replaced_ids)
    combined = [*custom, *remaining_builtins]
    combined.sort(key=lambda profile: profile.priority, reverse=True)
    return tuple(combined)


def load_semantic_profiles(
    path: str | Path = DEFAULT_SEMANTIC_OVERRIDES_PATH,
    schema_path: str | Path = DEFAULT_SEMANTIC_OVERRIDES_SCHEMA_PATH,
) -> tuple[SemanticProfile, ...]:
    """Load the profiles for this startup: built-ins when the file is absent, merged otherwise.

    Startup failures raise :class:`SemanticOverridesError` with the file and location so the
    operator sees exactly which entry is unusable; applying changes requires an application
    restart, which calls this loader again.
    """
    override_file = _resolve_override_path(path)
    if not override_file.exists():
        logger.info("Semantic overrides file absent at %s; using built-in profiles", override_file)
        return load_default_profiles()

    document = _read_json_mapping(override_file, label="Semantic overrides")
    _validate_schema(document, _resolve_override_path(schema_path), override_file)

    raw_profiles = document.get("profiles", [])
    seen_ids: set[str] = set()
    custom: list[SemanticProfile] = []
    for index, entry in enumerate(raw_profiles):
        profile = _parse_profile(entry, index, override_file)
        if profile.profile_id in seen_ids:
            raise SemanticOverridesError(
                f"Semantic overrides at {override_file} contain duplicate profile_id "
                f"'{profile.profile_id}' at profiles[{index}]; keep profile ids unique."
            )
        seen_ids.add(profile.profile_id)
        custom.append(profile)

    profiles = _merge_profiles(tuple(custom))
    logger.info(
        "Semantic profiles loaded from %s profiles=%s",
        override_file,
        ",".join(profile.profile_id for profile in profiles),
    )
    return profiles
