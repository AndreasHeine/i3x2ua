from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from asyncua import ua
from asyncua.ua.status_codes import StatusCodes

from i3x_server.schemas.opcua_value import (
    decode_data_value,
    format_utc_timestamp,
    normalize_opcua_value,
    notification_data_value,
    quality_from_status_code,
)


def _status_code(value: int) -> ua.StatusCode:
    return ua.StatusCode(ua.UInt32(value))


def _server_timestamp(moment: datetime) -> ua.DateTime:
    return ua.DateTime.fromisoformat(moment.astimezone(timezone.utc).isoformat())


@pytest.mark.parametrize("rank", [-3, -2, -1, 0])
def test_nonpositive_argument_rank_uses_null_dimensions(rank: int) -> None:
    argument = ua.Argument(ValueRank=rank, ArrayDimensions=[])
    normalized = normalize_opcua_value(argument)
    assert normalized.ArrayDimensions is None
    assert argument.ArrayDimensions == []
    assert normalize_opcua_value(normalized) is normalized


@pytest.mark.parametrize("dimensions", [None, [], [0], [10]])
def test_positive_argument_rank_preserves_dimensions(dimensions: list[int] | None) -> None:
    argument = ua.Argument(ValueRank=1, ArrayDimensions=dimensions)  # type: ignore[arg-type]
    assert normalize_opcua_value(argument) is argument


def test_nested_arguments_are_normalized_without_changing_other_arrays() -> None:
    @dataclass
    class Structure:
        body: Any

    argument = ua.Argument(ValueRank=-1, ArrayDimensions=[])
    original = Structure({"arguments": ([argument],), "empty": [], "null": None})
    normalized = normalize_opcua_value(original)
    assert normalized.body["arguments"][0][0].ArrayDimensions is None
    assert normalized.body["empty"] == []
    assert normalized.body["null"] is None
    assert original.body["arguments"][0][0].ArrayDimensions == []


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (_status_code(0x00000000), "Good"),
        (_status_code(0x40000000), "Uncertain"),
        (_status_code(0x80000000), "Bad"),
        (_status_code(int(StatusCodes.GoodNoData)), "GoodNoData"),
        (SimpleNamespace(name="UncertainSensorFailure"), "Uncertain"),
        (SimpleNamespace(name="BadNodeUnavailable"), "Bad"),
        (SimpleNamespace(is_good=lambda: True), "Good"),
        (SimpleNamespace(is_good=lambda: False), "Bad"),
        (None, None),
        (SimpleNamespace(), None),
    ],
)
def test_quality_from_status_code_maps_the_i3x_vocabulary(status_code: Any, expected: str | None) -> None:
    assert quality_from_status_code(status_code) == expected


def test_decode_data_value_reads_payload_quality_and_server_timestamp() -> None:
    data_value = ua.DataValue(ua.Variant(23.5, ua.VariantType.Double), _status_code(0x40000000))
    data_value.SourceTimestamp = _server_timestamp(datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    value, quality, timestamp = decode_data_value(data_value)
    assert value == 23.5
    assert quality == "Uncertain"
    assert timestamp == "2026-01-02T03:04:05Z"


def test_decode_data_value_falls_back_to_server_timestamp_only() -> None:
    data_value = ua.DataValue(ua.Variant(1.0, ua.VariantType.Double))
    data_value.ServerTimestamp = _server_timestamp(datetime(2026, 5, 6, 7, 8, tzinfo=timezone.utc))
    _value, quality, timestamp = decode_data_value(data_value)
    assert quality == "Good"
    assert timestamp == "2026-05-06T07:08:00Z"


def test_decode_data_value_reports_no_signal_for_missing_metadata() -> None:
    assert decode_data_value(None) == (None, None, None)
    value, quality, timestamp = decode_data_value(SimpleNamespace(Value=SimpleNamespace(Value=7), StatusCode=None))
    assert value == 7
    assert quality is None
    assert timestamp is None


def test_notification_data_value_extracts_the_monitored_item_value() -> None:
    data_value = ua.DataValue(ua.Variant(9.0, ua.VariantType.Double), _status_code(0x80000000))
    notification = SimpleNamespace(monitored_item=SimpleNamespace(ClientHandle=3, Value=data_value))
    assert notification_data_value(notification) is data_value
    assert notification_data_value(None) is None
    assert notification_data_value(SimpleNamespace(monitored_item=None)) is None


def test_format_utc_timestamp_renders_naive_and_zoned_values_as_utc() -> None:
    naive = datetime(2026, 1, 1, 12, 0)
    assert format_utc_timestamp(naive) == "2026-01-01T12:00:00Z"
    eastern = datetime(2026, 1, 1, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    assert format_utc_timestamp(eastern) == "2026-01-01T12:00:00Z"
