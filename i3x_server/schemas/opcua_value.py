from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timezone
from typing import Any

from asyncua.ua.status_codes import StatusCodes
from asyncua.ua.uaprotocol_auto import Argument

from i3x_server.domain.ports.opcua import DataValueDTO


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


def format_utc_timestamp(value: datetime) -> str:
    """Render a timestamp the way the i3X contract documents it (UTC, ``Z`` suffix)."""
    normalized = value
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def quality_from_status_code(status_code: Any) -> str | None:
    """Map an OPC UA StatusCode onto ``Good``/``Uncertain``/``Bad``/``GoodNoData``.

    Returns ``None`` when the code carries no usable signal, so callers can fall back
    instead of inventing a quality. Numeric codes are decoded from the severity bits;
    codes without a numeric value (test doubles, name-only stubs) are matched on their
    ``is_good``/``is_uncertain`` helpers or name.
    """
    if status_code is None:
        return None

    numeric = getattr(status_code, "value", None)
    if numeric is not None:
        try:
            code = int(numeric)
        except (TypeError, ValueError):
            return None
        if code == int(StatusCodes.GoodNoData):
            return "GoodNoData"
        severity = code & 0xC0000000
        if severity == 0x00000000:
            return "Good"
        if severity == 0x40000000:
            return "Uncertain"
        if severity == 0x80000000:
            return "Bad"
        return None

    is_uncertain = getattr(status_code, "is_uncertain", None)
    is_bad = getattr(status_code, "is_bad", None)
    is_good = getattr(status_code, "is_good", None)
    for helper, quality in ((is_uncertain, "Uncertain"), (is_bad, "Bad"), (is_good, "Good")):
        if callable(helper):
            try:
                if bool(helper()):
                    return quality
            except Exception:
                continue
    if any(callable(helper) for helper in (is_uncertain, is_bad, is_good)):
        return "Bad"

    name = str(getattr(status_code, "name", "") or "")
    if not name:
        return None
    lowered = name.lower()
    if "uncertain" in lowered:
        return "Uncertain"
    if "bad" in lowered:
        return "Bad"
    if "nodata" in lowered:
        return "GoodNoData"
    if "good" in lowered:
        return "Good"
    return "Bad"


def severity_quality(status_code: int | None) -> str:
    """Map a numeric status code onto ``Good``/``Uncertain``/``Bad`` by severity bits.

    VQT consumers that treat every Good-severity code (including ``GoodNoData``) as
    ``Good`` use this instead of :func:`quality_from_status_code`, which keeps the
    ``GoodNoData`` distinction.
    """
    if status_code is None:
        return "Good"
    severity = int(status_code) & 0xC0000000
    if severity == 0x40000000:
        return "Uncertain"
    if severity == 0x80000000:
        return "Bad"
    if severity == 0x00000000:
        return "Good"
    return "Bad"


def decode_data_value(data_value: DataValueDTO | None) -> tuple[Any, str | None, str | None]:
    """Split a data-value DTO into payload, quality, and the server-provided timestamp.

    ``quality``/``timestamp`` are ``None`` when the value carries no signal; the server
    timestamp prefers ``SourceTimestamp`` and falls back to ``ServerTimestamp``.
    """
    if data_value is None:
        return None, None, None

    payload = data_value.value.value if data_value.value is not None else None
    timestamp = data_value.source_timestamp or data_value.server_timestamp
    if isinstance(timestamp, datetime):
        return payload, data_value.quality, format_utc_timestamp(timestamp)
    return payload, data_value.quality, None
