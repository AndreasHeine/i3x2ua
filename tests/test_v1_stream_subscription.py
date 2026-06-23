from __future__ import annotations

from typing import cast

import pytest
from fastapi import HTTPException
from starlette.responses import StreamingResponse

from i3x_server.api.v1.monolithic import StreamRequest, stream_subscription_v1
from i3x_server.application.ports.subscription import SubscriptionServicePort
from i3x_server.domain.ports.opcua import OpcUaClientProtocol, OpcUaNamespaceInfo
from i3x_server.infrastructure.subscriptions.service import SubscriptionSyncResult, SubscriptionUpdate


class _FakeOpcUaClient:
    async def get_namespace_infos(self) -> list[OpcUaNamespaceInfo]:
        return [
            OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="UA"),
            OpcUaNamespaceInfo(uri="http://example.com/custom", display_name="Custom"),
        ]


class _FakeSubscriptionService:
    def __init__(self) -> None:
        self.activate_returns: list[int | None] = [1]
        self.sync_result: SubscriptionSyncResult | None = None
        self.active_sequence: list[bool] = [False]
        self.wait_returns: list[list[SubscriptionUpdate] | None] = []
        self.deactivated: list[tuple[str, int]] = []

    async def activate_stream(self, client_id: str | None, subscription_id: str) -> int | None:
        del client_id, subscription_id
        return self.activate_returns.pop(0) if self.activate_returns else None

    async def sync(
        self,
        client_id: str | None,
        subscription_id: str,
        acknowledge_sequence: int | None = None,
        allow_when_stream_active: bool = False,
    ) -> SubscriptionSyncResult | None:
        del client_id, subscription_id, acknowledge_sequence, allow_when_stream_active
        return self.sync_result

    async def is_stream_active(self, subscription_id: str, stream_generation: int) -> bool:
        del subscription_id, stream_generation
        return self.active_sequence.pop(0) if self.active_sequence else False

    async def wait_for_updates(
        self,
        client_id: str | None,
        subscription_id: str,
        after_sequence: int,
        timeout_seconds: float,
    ) -> list[SubscriptionUpdate] | None:
        del client_id, subscription_id, after_sequence, timeout_seconds
        return self.wait_returns.pop(0) if self.wait_returns else []

    async def deactivate_stream(self, subscription_id: str, stream_generation: int) -> None:
        self.deactivated.append((subscription_id, stream_generation))


@pytest.mark.asyncio
async def test_stream_subscription_raises_not_found_when_activation_fails() -> None:
    service = _FakeSubscriptionService()
    service.activate_returns = [None, None]
    service.sync_result = SubscriptionSyncResult(updates=[])
    body = StreamRequest(clientId="client-1", subscriptionId="sub-missing", acknowledgeSequence=0)

    with pytest.raises(HTTPException) as exc_info:
        await stream_subscription_v1(
            body,
            opcua_client=cast(OpcUaClientProtocol, _FakeOpcUaClient()),
            subscription_service=cast(SubscriptionServicePort, service),
        )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_stream_subscription_sends_initial_payload_then_close() -> None:
    service = _FakeSubscriptionService()
    service.activate_returns = [1]
    service.sync_result = SubscriptionSyncResult(
        updates=[
            SubscriptionUpdate(
                sequence_number=2,
                element_id="ns=1;i=42",
                node_id="ns=1;i=42",
                value={"k": "v"},
                quality="Good",
                timestamp="2026-01-01T00:00:00Z",
            )
        ]
    )
    service.active_sequence = [False]
    body = StreamRequest(clientId="client-1", subscriptionId="sub-1", acknowledgeSequence=0)

    response = await stream_subscription_v1(
        body,
        opcua_client=cast(OpcUaClientProtocol, _FakeOpcUaClient()),
        subscription_service=cast(SubscriptionServicePort, service),
    )
    assert isinstance(response, StreamingResponse)

    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(str(chunk))

    assert any(": connected" in chunk for chunk in chunks)
    assert any("data:" in chunk and "sequenceNumber" in chunk for chunk in chunks)
    assert any("event: close" in chunk for chunk in chunks)
    assert service.deactivated == [("sub-1", 1)]


@pytest.mark.asyncio
async def test_stream_subscription_keepalive_and_update_flow() -> None:
    service = _FakeSubscriptionService()
    service.activate_returns = [None, 7]
    service.sync_result = SubscriptionSyncResult(updates=[])
    service.active_sequence = [True, True, False]
    service.wait_returns = [
        [],
        [
            SubscriptionUpdate(
                sequence_number=5,
                element_id="ns=1;i=100",
                node_id="ns=1;i=100",
                value=123,
                quality="Good",
                timestamp="2026-01-01T00:01:00Z",
            )
        ],
    ]
    body = StreamRequest(clientId="client-1", subscriptionId="sub-2", acknowledgeSequence=1)

    response = await stream_subscription_v1(
        body,
        opcua_client=cast(OpcUaClientProtocol, _FakeOpcUaClient()),
        subscription_service=cast(SubscriptionServicePort, service),
    )
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(str(chunk))

    assert any(": keepalive" in chunk for chunk in chunks)
    assert any("data:" in chunk and "nsu=http://example.com/custom;i=100" in chunk for chunk in chunks)
    assert any("event: close" in chunk for chunk in chunks)
    assert service.deactivated == [("sub-2", 7)]


@pytest.mark.asyncio
async def test_stream_subscription_raises_not_found_when_ack_sync_missing() -> None:
    service = _FakeSubscriptionService()
    service.activate_returns = [2]
    service.sync_result = None
    body = StreamRequest(clientId="client-1", subscriptionId="sub-3", acknowledgeSequence=0)

    with pytest.raises(HTTPException) as exc_info:
        await stream_subscription_v1(
            body,
            opcua_client=cast(OpcUaClientProtocol, _FakeOpcUaClient()),
            subscription_service=cast(SubscriptionServicePort, service),
        )
    assert exc_info.value.status_code == 404
