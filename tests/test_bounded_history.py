from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from asyncua import ua
from asyncua.ua.status_codes import StatusCodes
from asyncua.ua.uaerrors import UaStatusCodeError

from i3x_server.infrastructure.opcua.client import OpcUaClient


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation, count, truncated", [(b"page", 2, True), (None, 3, True), (None, 1, False)])
async def test_bounded_history_requests_one_page_and_releases_continuation(
    monkeypatch: pytest.MonkeyPatch, continuation: bytes | None, count: int, truncated: bool
) -> None:
    calls: list[Any] = []
    values = [ua.DataValue(ua.Variant(index)) for index in range(count)]

    class Session:
        async def history_read(self, parameters: ua.HistoryReadParameters) -> list[ua.HistoryReadResult]:
            calls.append(parameters)
            assert parameters.ReleaseContinuationPoints is True
            assert parameters.NodesToRead[0].ContinuationPoint == b"page"
            return [ua.HistoryReadResult()]

    class Node:
        nodeid = ua.NodeId(ua.String("temperature"), ua.Int16(2))
        session = Session()

        async def history_read(self, details: ua.ReadRawModifiedDetails) -> ua.HistoryReadResult:
            calls.append(details)
            assert details.NumValuesPerNode == 3
            assert details.ReturnBounds is False
            return ua.HistoryReadResult(HistoryData=ua.HistoryData(DataValues=values), ContinuationPoint=continuation)

    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    monkeypatch.setattr(client._client, "get_node", lambda node_id: Node())
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = await client.read_bounded_history("ns=2;s=temperature", start, start, 3)
    assert result.values == values
    assert result.truncated is truncated
    assert len(calls) == (2 if continuation else 1)


@pytest.mark.asyncio
async def test_bounded_history_keeps_unsupported_status_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    class Node:
        async def history_read(self, details: ua.ReadRawModifiedDetails) -> ua.HistoryReadResult:
            return ua.HistoryReadResult(StatusCode=ua.StatusCode(ua.UInt32(StatusCodes.BadHistoryOperationUnsupported)))

    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    monkeypatch.setattr(client._client, "get_node", lambda node_id: Node())
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(UaStatusCodeError):
        await client.read_bounded_history("ns=2;s=temperature", start, start, 3)
    with pytest.raises(ValueError):
        await client.read_bounded_history("ns=2;s=temperature", start, start, 0)
