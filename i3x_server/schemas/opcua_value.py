from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from typing import Any

from asyncua.ua.uaprotocol_auto import Argument


def normalize_opcua_value(value: Any) -> Any:
    """Canonicalize Argument dimensions without mutating upstream values."""
    if isinstance(value, Argument):
        if value.ValueRank <= 0 and value.ArrayDimensions is not None:
            # asyncua's annotation omits the null value required by OPC UA Part 3, 8.6.
            return replace(value, ArrayDimensions=None)  # type: ignore[arg-type]
        return value
    if isinstance(value, list):
        return [normalize_opcua_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(normalize_opcua_value(item) for item in value)
    if isinstance(value, dict):
        return {key: normalize_opcua_value(item) for key, item in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        changes = {}
        for item in fields(value):
            if not item.init:
                continue
            original = getattr(value, item.name)
            normalized = normalize_opcua_value(original)
            if normalized is not original:
                changes[item.name] = normalized
        if changes:
            return replace(value, **changes)
    return value
