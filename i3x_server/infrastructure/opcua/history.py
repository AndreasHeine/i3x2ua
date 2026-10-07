from __future__ import annotations

import base64
import binascii
import math
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from asyncua import ua
from asyncua.ua.status_codes import StatusCodes

_INTEGER_BITS = {
    ua.VariantType.SByte: (8, True),
    ua.VariantType.Byte: (8, False),
    ua.VariantType.Int16: (16, True),
    ua.VariantType.UInt16: (16, False),
    ua.VariantType.Int32: (32, True),
    ua.VariantType.UInt32: (32, False),
    ua.VariantType.Int64: (64, True),
    ua.VariantType.UInt64: (64, False),
}
_QUALITY_CODES = {
    "Good": StatusCodes.Good,
    "Uncertain": StatusCodes.Uncertain,
    "Bad": StatusCodes.Bad,
    "GoodNoData": StatusCodes.GoodNoData,
}
_SUPPORTED_TYPES = set(_INTEGER_BITS) | {
    ua.VariantType.Boolean,
    ua.VariantType.Float,
    ua.VariantType.Double,
    ua.VariantType.String,
    ua.VariantType.DateTime,
    ua.VariantType.Guid,
    ua.VariantType.ByteString,
}


def _convert_scalar(value: Any, variant_type: ua.VariantType) -> Any:
    if variant_type in _INTEGER_BITS:
        bits, signed = _INTEGER_BITS[variant_type]
        minimum = -(2 ** (bits - 1)) if signed else 0
        maximum = 2 ** (bits - (1 if signed else 0)) - 1
        if isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum:
            return value
    elif variant_type in {ua.VariantType.Float, ua.VariantType.Double}:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                number = float(value)
            except OverflowError as exc:
                raise ValueError("Numeric value is out of range") from exc
            limit = 3.4028234663852886e38 if variant_type == ua.VariantType.Float else float("inf")
            if math.isfinite(number) and abs(number) <= limit:
                return number
    elif variant_type == ua.VariantType.Boolean:
        if isinstance(value, bool):
            return value
    elif variant_type == ua.VariantType.String:
        if isinstance(value, str):
            return value
    elif variant_type == ua.VariantType.DateTime:
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc)
    elif variant_type == ua.VariantType.Guid:
        if isinstance(value, str):
            return UUID(value)
    elif variant_type == ua.VariantType.ByteString:
        if isinstance(value, dict) and set(value) == {"encoding", "data"} and value["encoding"] == "base64":
            if isinstance(value["data"], str):
                try:
                    return base64.b64decode(value["data"], validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise ValueError("Invalid base64 ByteString") from exc
    else:
        raise ValueError(f"Historical writes for {variant_type.name} values are not supported")
    raise ValueError(f"Value does not conform to OPC UA {variant_type.name}")


def _convert_value(value: Any, variant_type: ua.VariantType) -> tuple[Any, list[int]]:
    if not isinstance(value, list):
        return _convert_scalar(value, variant_type), []
    converted = [_convert_value(item, variant_type) for item in value]
    child_shape = converted[0][1] if converted else []
    if any(shape != child_shape for _, shape in converted):
        raise ValueError("Arrays must be rectangular and contain values of the declared type")
    return [item for item, _ in converted], [len(value), *child_shape]


def historical_data_value(
    value: Any,
    quality: str,
    timestamp: datetime,
    variant_type: ua.VariantType,
    value_rank: int,
    array_dimensions: list[int] | None,
) -> ua.DataValue:
    if quality not in _QUALITY_CODES:
        raise ValueError("Unsupported historical quality")
    if timestamp.tzinfo is None:
        raise ValueError("Historical timestamp must include a timezone")
    if value is None:
        if quality not in {"Bad", "GoodNoData"}:
            raise ValueError("A null value requires Bad or GoodNoData quality")
        variant = ua.Variant(None, ua.VariantType.Null)
    else:
        if quality in {"Bad", "GoodNoData"}:
            raise ValueError("Bad and GoodNoData quality require a null value")
        if variant_type not in _SUPPORTED_TYPES:
            raise ValueError(f"Historical writes for {variant_type.name} values are not supported")
        converted, shape = _convert_value(value, variant_type)
        dimensions = len(shape)
        if (
            (value_rank == -1 and dimensions != 0)
            or (value_rank == -3 and dimensions > 1)
            or (value_rank == 0 and dimensions == 0)
            or (value_rank > 0 and dimensions != value_rank)
            or value_rank < -3
        ):
            raise ValueError("Value does not conform to the OPC UA ValueRank")
        if shape and array_dimensions:
            if len(shape) != len(array_dimensions) or any(
                expected != 0 and actual > expected for actual, expected in zip(shape, array_dimensions, strict=True)
            ):
                raise ValueError("Value does not conform to OPC UA ArrayDimensions")
        variant = ua.Variant(converted, variant_type)
    return ua.DataValue(
        Value=variant,
        StatusCode=ua.StatusCode(ua.UInt32(_QUALITY_CODES[quality])),
        SourceTimestamp=ua.DateTime.fromisoformat(timestamp.astimezone(timezone.utc).isoformat()),
    )
