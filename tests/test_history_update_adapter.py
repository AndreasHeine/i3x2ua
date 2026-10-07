from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from asyncua import ua
from asyncua.server.server import Server
from asyncua.ua import ua_binary
from asyncua.ua.attribute_ids import AttributeIds
from asyncua.ua.status_codes import StatusCodes
from asyncua.ua.uaerrors import BadNotWritable, UaError

from i3x_server.infrastructure.opcua.client import OpcUaClient
from i3x_server.infrastructure.opcua.history import historical_data_value

TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _attribute(value: Any) -> ua.DataValue:
    return ua.DataValue(Value=ua.Variant(value, ua.VariantType.Int32))


def _node() -> SimpleNamespace:
    return SimpleNamespace(
        nodeid=ua.NodeId.from_string("ns=2;s=Temperature"),
        read_data_type_as_variant_type=AsyncMock(return_value=ua.VariantType.Double),
        read_attributes=AsyncMock(return_value=[_attribute(-1), _attribute([])]),
        history_update=AsyncMock(return_value=ua.HistoryUpdateResult(OperationResults=[ua.StatusCode()])),
    )


@pytest.mark.asyncio
async def test_adapter_submits_full_vqt(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpcUaClient("opc.tcp://localhost:4840")
    node = _node()
    monkeypatch.setattr(client, "_client", SimpleNamespace(get_node=lambda _: node))
    await client.write_history_value("ns=2;s=Temperature", 19, "Uncertain", TIMESTAMP)
    details = node.history_update.call_args.args[0]
    assert details.NodeId == node.nodeid
    assert details.PerformInsertReplace == ua.PerformUpdateType.Update
    assert len(details.UpdateValues) == 1
    data_value = details.UpdateValues[0]
    assert data_value.Value.VariantType == ua.VariantType.Double
    assert data_value.Value.Value == 19.0
    assert data_value.StatusCode.value == StatusCodes.Uncertain
    assert data_value.SourceTimestamp == TIMESTAMP
    assert data_value.ServerTimestamp is None
    assert client.snapshot_request_metrics().history_write_count == 1
    assert client.snapshot_request_metrics().write_count == 0


@pytest.mark.asyncio
async def test_actual_asyncua_server_rejection_is_not_reported_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    server = Server()
    await server.init()
    namespace = await server.register_namespace("urn:i3x2ua:history-test")
    node = await server.nodes.objects.add_variable(namespace, "Temperature", 42.5)
    for attribute in (AttributeIds.AccessLevel, AttributeIds.UserAccessLevel):
        await node.write_attribute(attribute, ua.DataValue(Value=ua.Variant(9, ua.VariantType.Byte)))
    client = OpcUaClient("opc.tcp://localhost:4840")
    monkeypatch.setattr(client, "_client", server)
    node_id = node.nodeid.to_string()
    assert await client.read_history_write_access(node_id) == (True, True)
    with pytest.raises(BadNotWritable):
        await client.write_history_value(node_id, 19.5, "Good", TIMESTAMP)
    assert await node.read_value() == 42.5
    assert client.snapshot_request_metrics().history_write_count == 0
    assert client.snapshot_request_metrics().failed_request_count == 1


@pytest.mark.asyncio
async def test_adapter_accepts_absent_optional_array_dimensions(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpcUaClient("opc.tcp://localhost:4840")
    node = _node()
    node.read_attributes.return_value = [
        _attribute(-1),
        ua.DataValue(StatusCode=ua.StatusCode(ua.UInt32(StatusCodes.BadAttributeIdInvalid))),
    ]
    monkeypatch.setattr(client, "_client", SimpleNamespace(get_node=lambda _: node))
    await client.write_history_value("ns=2;s=Temperature", 19, "Good", TIMESTAMP)
    assert client.snapshot_request_metrics().history_write_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["access", "type"])
@pytest.mark.parametrize("mode", ["missing", "status", "bad"])
async def test_adapter_rejects_incomplete_metadata(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    mode: str,
) -> None:
    client = OpcUaClient("opc.tcp://localhost:4840")
    node = _node()
    attributes = [_attribute(8), _attribute(8)]
    if mode == "missing":
        attributes.pop()
    elif mode == "status":
        attributes[0].StatusCode = None
    else:
        attributes[0].StatusCode = ua.StatusCode(ua.UInt32(StatusCodes.BadUserAccessDenied))
    node.read_attributes.return_value = attributes
    monkeypatch.setattr(client, "_client", SimpleNamespace(get_node=lambda _: node))
    with pytest.raises(UaError):
        if operation == "access":
            await client.read_history_write_access("ns=2;s=Temperature")
        else:
            await client.write_history_value("ns=2;s=Temperature", 19, "Good", TIMESTAMP)
    node.history_update.assert_not_awaited()
    assert client.snapshot_request_metrics().failed_request_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["operation", "overall", "missing", "timeout", "invalid"])
async def test_adapter_rejects_failed_results_without_retry(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    client = OpcUaClient("opc.tcp://localhost:4840")
    node = _node()
    if mode == "operation":
        node.history_update.return_value.OperationResults = [ua.StatusCode(ua.UInt32(StatusCodes.BadUserAccessDenied))]
    elif mode == "overall":
        node.history_update.return_value.StatusCode = ua.StatusCode(ua.UInt32(StatusCodes.BadNotWritable))
    elif mode == "missing":
        node.history_update.return_value.OperationResults = []
    elif mode == "timeout":
        node.history_update.side_effect = TimeoutError("timed out")
    monkeypatch.setattr(client, "_client", SimpleNamespace(get_node=lambda _: node))
    with pytest.raises((UaError, TimeoutError, ValueError)):
        await client.write_history_value("ns=2;s=Temperature", "bad" if mode == "invalid" else 19, "Good", TIMESTAMP)
    assert node.history_update.await_count == (0 if mode == "invalid" else 1)
    assert client.snapshot_request_metrics().history_write_count == 0
    assert client.snapshot_request_metrics().failed_request_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("access,user_access", [(2, 2), (8, 2), (2, 8), (8, 8)])
async def test_history_access_uses_history_bit(
    monkeypatch: pytest.MonkeyPatch,
    access: int,
    user_access: int,
) -> None:
    client = OpcUaClient("opc.tcp://localhost:4840")
    node = _node()
    node.read_attributes.return_value = [_attribute(access), _attribute(user_access)]
    monkeypatch.setattr(client, "_client", SimpleNamespace(get_node=lambda _: node))
    assert await client.read_history_write_access("ns=2;s=Temperature") == (bool(access & 8), bool(user_access & 8))


@pytest.mark.parametrize(
    ("value", "variant_type", "rank", "dimensions", "expected"),
    [
        (True, ua.VariantType.Boolean, -1, None, True),
        (255, ua.VariantType.Byte, -1, None, 255),
        ("text", ua.VariantType.String, -1, None, "text"),
        ("2026-01-01T01:00:00+01:00", ua.VariantType.DateTime, -1, None, TIMESTAMP),
        ("00000000-0000-0000-0000-000000000001", ua.VariantType.Guid, -1, None, UUID(int=1)),
        ({"encoding": "base64", "data": "/wA="}, ua.VariantType.ByteString, -1, None, b"\xff\x00"),
        ([1, 2], ua.VariantType.Int16, 1, [2], [1, 2]),
        ([1, 2], ua.VariantType.Int16, 1, [3], [1, 2]),
        ([[1, 2], [3, 4]], ua.VariantType.Double, 2, [0, 2], [[1.0, 2.0], [3.0, 4.0]]),
        ([], ua.VariantType.String, 1, [0], []),
    ],
)
def test_history_value_conversion(
    value: Any,
    variant_type: ua.VariantType,
    rank: int,
    dimensions: list[int] | None,
    expected: Any,
) -> None:
    result = historical_data_value(value, "Good", TIMESTAMP, variant_type, rank, dimensions)
    assert result.Value is not None
    assert result.Value.Value == expected
    assert result.Value.VariantType == variant_type
    assert ua_binary.struct_to_binary(
        ua.UpdateDataDetails(
            NodeId=ua.NodeId.from_string("ns=2;s=Temperature"),
            PerformInsertReplace=ua.PerformUpdateType.Update,
            UpdateValues=[result],
        )
    )


@pytest.mark.parametrize(
    ("value", "variant_type", "rank", "dimensions"),
    [
        (256, ua.VariantType.Byte, -1, None),
        (-1, ua.VariantType.UInt32, -1, None),
        (True, ua.VariantType.Int32, -1, None),
        (1, ua.VariantType.Boolean, -1, None),
        (1, ua.VariantType.String, -1, None),
        (float("inf"), ua.VariantType.Double, -1, None),
        (1e39, ua.VariantType.Float, -1, None),
        (10**400, ua.VariantType.Double, -1, None),
        ("2026-01-01T00:00:00", ua.VariantType.DateTime, -1, None),
        ("bad", ua.VariantType.Guid, -1, None),
        ({"encoding": "base64", "data": "!"}, ua.VariantType.ByteString, -1, None),
        ("abc", ua.VariantType.ByteString, -1, None),
        ({}, ua.VariantType.ExtensionObject, -1, None),
        ([], ua.VariantType.ExtensionObject, 1, None),
        ([1, 2], ua.VariantType.Double, -1, None),
        (1, ua.VariantType.Double, 1, None),
        (1, ua.VariantType.Double, 0, None),
        ([1, 2], ua.VariantType.Double, 1, [1]),
        ([1, 2], ua.VariantType.Double, 1, [2, 2]),
        ([[1], [2, 3]], ua.VariantType.Double, 2, None),
        ([[1]], ua.VariantType.Double, -3, None),
        (1, ua.VariantType.Double, -4, None),
    ],
)
def test_history_value_rejects_type_rank_and_range_errors(
    value: Any,
    variant_type: ua.VariantType,
    rank: int,
    dimensions: list[int] | None,
) -> None:
    with pytest.raises(ValueError):
        historical_data_value(value, "Good", TIMESTAMP, variant_type, rank, dimensions)


def test_history_null_quality_and_timezone() -> None:
    for quality in ("Bad", "GoodNoData"):
        result = historical_data_value(None, quality, TIMESTAMP, ua.VariantType.Double, -1, None)
        assert result.Value is not None
        assert result.Value.VariantType == ua.VariantType.Null
    for value, quality, timestamp in (
        (None, "Good", TIMESTAMP),
        (1, "GoodNoData", TIMESTAMP),
        (1, "Bad", TIMESTAMP),
        (1, "unknown", TIMESTAMP),
        (1, "Good", TIMESTAMP.replace(tzinfo=None)),
    ):
        with pytest.raises(ValueError):
            historical_data_value(value, quality, timestamp, ua.VariantType.Double, -1, None)
