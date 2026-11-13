"""Timestamp, quality, status-code, and payload normalization shared by the runtime.

One implementation of each rule, so a response, an update, and a write report the same value."""

from __future__ import annotations

import base64
from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timezone
from typing import Any

from asyncua.ua.status_codes import StatusCodes
from asyncua.ua.uaprotocol_auto import Argument
from pydantic import BaseModel

from i3x_server.domain.ports.opcua import DataValueDTO, Quality

# OPC UA StatusCode severity bits (Part 6, 7.16): the top two bits carry the severity, and
# ``GoodNoData`` is a Good-severity code whose low bits mark "no data".
_SEVERITY_MASK = 0xC0000000
_SEVERITY_GOOD = 0x00000000
_SEVERITY_UNCERTAIN = 0x40000000
_SEVERITY_BAD = 0x80000000


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


def now_iso() -> str:
    """Current time as an i3X UTC timestamp (``Z`` suffix)."""
    return format_utc_timestamp(datetime.now(timezone.utc))


def normalize_timestamp(value: Any) -> str:
    """Timestamp for a payload field: the value when it is a datetime, else now.

    Upstream attributes may carry no source/server timestamp; reporting the gateway's clock
    keeps the i3X VQT contract's ``timestamp`` field populated.
    """
    if isinstance(value, datetime):
        return format_utc_timestamp(value)
    return now_iso()


def status_code_of(exc: BaseException) -> int | None:
    """Raw OPC UA status code carried by an exception, ``None`` when it carries none.

    ``asyncua.ua.uaerrors.UaStatusCodeError`` subclasses expose ``code`` as a plain ``int``;
    ``ua.StatusCode`` carriers expose ``value``. The runtime stays free of an asyncua import
    at the call sites, so the code is read structurally.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    value = getattr(code, "value", None)
    return value if isinstance(value, int) else None


def quality_from_status_code(status_code: Any) -> Quality | None:
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
            return Quality.GOOD_NO_DATA
        severity = code & _SEVERITY_MASK
        if severity == _SEVERITY_GOOD:
            return Quality.GOOD
        if severity == _SEVERITY_UNCERTAIN:
            return Quality.UNCERTAIN
        if severity == _SEVERITY_BAD:
            return Quality.BAD
        return None

    is_uncertain = getattr(status_code, "is_uncertain", None)
    is_bad = getattr(status_code, "is_bad", None)
    is_good = getattr(status_code, "is_good", None)
    for helper, quality in ((is_uncertain, Quality.UNCERTAIN), (is_bad, Quality.BAD), (is_good, Quality.GOOD)):
        if callable(helper):
            try:
                if bool(helper()):
                    return quality
            except Exception:
                continue
    if any(callable(helper) for helper in (is_uncertain, is_bad, is_good)):
        return Quality.BAD

    name = str(getattr(status_code, "name", "") or "")
    if not name:
        return None
    lowered = name.lower()
    if "uncertain" in lowered:
        return Quality.UNCERTAIN
    if "bad" in lowered:
        return Quality.BAD
    if "nodata" in lowered:
        return Quality.GOOD_NO_DATA
    if "good" in lowered:
        return Quality.GOOD
    return Quality.BAD


def severity_quality(status_code: int | None) -> Quality:
    """Map a numeric status code onto ``Good``/``Uncertain``/``Bad`` by severity bits.

    VQT consumers that treat every Good-severity code (including ``GoodNoData``) as
    ``Good`` use this instead of :func:`quality_from_status_code`, which keeps the
    ``GoodNoData`` distinction.
    """
    if status_code is None:
        return Quality.GOOD
    severity = int(status_code) & _SEVERITY_MASK
    if severity == _SEVERITY_UNCERTAIN:
        return Quality.UNCERTAIN
    if severity == _SEVERITY_BAD:
        return Quality.BAD
    if severity == _SEVERITY_GOOD:
        return Quality.GOOD
    return Quality.BAD


def quality_for_payload(value: Any, quality: Quality | None) -> Quality:
    """The quality a payload must report, keeping the null-value rule of the VQT contract.

    A null payload never reports ``Good``/``Uncertain``: it becomes ``GoodNoData`` (a
    Good-severity value without data). ``Bad``/``GoodNoData`` are kept as they are, and a
    missing signal defaults to ``GoodNoData`` for a null payload and ``Good`` otherwise.
    """
    resolved = quality or (Quality.GOOD_NO_DATA if value is None else Quality.GOOD)
    if value is None and resolved in {Quality.GOOD, Quality.UNCERTAIN}:
        return Quality.GOOD_NO_DATA
    return resolved


def decode_data_value(data_value: DataValueDTO | None) -> tuple[Any, Quality | None, str | None]:
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


def _is_encoding_field(name: str) -> bool:
    """Whether a structure field carries the OPC UA encoding type rather than payload data."""
    return name.strip().lower() == "encoding"


def to_json_safe_value(value: Any, *, exclude_encoding_fields: bool = False) -> Any:
    """Convert an upstream value into something JSON-serializable.

    ByteStrings use the gateway's base64 convention (the write path expects base64, so a
    ``str(b'...')`` repr would break a round-trip), extension objects keep the
    ``TypeId``/``Body`` shape, and datetimes render as UTC ``Z`` timestamps. Anything else
    degrades to its string form rather than raising.

    ``exclude_encoding_fields`` drops ``encoding`` fields from structures and plain objects:
    the presentation layer must not publish the OPC UA encoding type alongside the value.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return format_utc_timestamp(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if hasattr(value, "TypeId") and hasattr(value, "Body"):
        body = value.Body
        if body is None:
            return None
        return {
            "TypeId": to_json_safe_value(value.TypeId, exclude_encoding_fields=exclude_encoding_fields),
            "Body": to_json_safe_value(body, exclude_encoding_fields=exclude_encoding_fields),
        }
    if isinstance(value, BaseModel):
        return to_json_safe_value(
            value.model_dump(mode="json", by_alias=True),
            exclude_encoding_fields=exclude_encoding_fields,
        )
    if is_dataclass(value):
        value = normalize_opcua_value(value)
        return {
            item.name: to_json_safe_value(getattr(value, item.name), exclude_encoding_fields=exclude_encoding_fields)
            for item in fields(value)
            if not (exclude_encoding_fields and _is_encoding_field(item.name))
        }
    if isinstance(value, (list, tuple, set)):
        return [to_json_safe_value(item, exclude_encoding_fields=exclude_encoding_fields) for item in value]
    if isinstance(value, dict):
        return {
            str(key): to_json_safe_value(item, exclude_encoding_fields=exclude_encoding_fields)
            for key, item in value.items()
        }
    if hasattr(value, "__dict__") and type(value).__module__ != "builtins":
        return {
            str(key): to_json_safe_value(item, exclude_encoding_fields=exclude_encoding_fields)
            for key, item in vars(value).items()
            if not key.startswith("_")
            and not callable(item)
            and not (exclude_encoding_fields and _is_encoding_field(str(key)))
        }
    return str(value)
