from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from i3x_server.application.ports.subscription import SubscriptionServicePort
from i3x_server.application.services.subscription import SubscriptionAppService
from i3x_server.domain.ports.opcua import OpcUaClientProtocol
from i3x_server.infrastructure.subscriptions.service import (
    SubscriptionDeleteResult,
    SubscriptionDetail,
    SubscriptionSyncResult,
    SubscriptionUpdate,
)
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult


def _model() -> BuildResult:
    node = ModelNode(
        id="asset-root",
        name="Root",
        kind="asset",
        type=None,
        children=[],
        source_node_id="ns=2;s=Root",
    )
    return BuildResult(
        nodes_by_id={node.id: node},
        root_ids=[node.id],
        children_by_id={node.id: []},
        instances_by_type_id={},
        property_to_node={},
        action_to_method={},
    )


def _request() -> Request:
    app = FastAPI()
    return Request({"type": "http", "app": app})


class _FakeSubscriptionPort:
    def __init__(self) -> None:
        self.register_result = True
        self.sync_result: SubscriptionSyncResult | None = None
        self.delete_results: list[SubscriptionDeleteResult] = []
        self.list_results: list[SubscriptionDetail] = []
        self.create_error: Exception | None = None
        self.register_error: Exception | None = None
        self.sync_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.list_error: Exception | None = None

    async def create_subscription(self, client_id: str | None, display_name: str | None = None) -> SubscriptionDetail:
        if self.create_error:
            raise self.create_error
        return SubscriptionDetail(
            subscription_id="sub-1",
            client_id=client_id,
            display_name=display_name,
            monitored_objects=[],
            mode="poll",
        )

    async def register_items(
        self,
        client_id: str | None,
        subscription_id: str,
        element_ids: list[str],
        max_depth: int,
        model: BuildResult,
    ) -> bool:
        del client_id, subscription_id, element_ids, max_depth, model
        if self.register_error:
            raise self.register_error
        return self.register_result

    async def sync(
        self,
        client_id: str | None,
        subscription_id: str,
        acknowledge_sequence: int | None = None,
        allow_when_stream_active: bool = False,
    ) -> SubscriptionSyncResult | None:
        del client_id, subscription_id, acknowledge_sequence, allow_when_stream_active
        if self.sync_error:
            raise self.sync_error
        return self.sync_result

    async def delete_subscriptions(
        self,
        client_id: str | None,
        subscription_ids: list[str],
    ) -> list[SubscriptionDeleteResult]:
        del client_id, subscription_ids
        if self.delete_error:
            raise self.delete_error
        return self.delete_results

    async def list_subscriptions(
        self,
        client_id: str | None,
        subscription_ids: list[str] | None = None,
    ) -> list[SubscriptionDetail]:
        del client_id, subscription_ids
        if self.list_error:
            raise self.list_error
        return self.list_results


@pytest.mark.asyncio
async def test_create_subscription_success() -> None:
    port = _FakeSubscriptionPort()
    service = SubscriptionAppService(
        opcua_client=cast(OpcUaClientProtocol, SimpleNamespace()),
        model=_model(),
        subscription_service=cast(SubscriptionServicePort, port),
    )
    dto = await service.create_subscription("client-1", "Demo")
    assert dto == {"subscriptionId": "sub-1", "clientId": "client-1", "displayName": "Demo"}


@pytest.mark.asyncio
async def test_create_subscription_wraps_errors() -> None:
    port = _FakeSubscriptionPort()
    port.create_error = RuntimeError("boom")
    service = SubscriptionAppService(
        cast(OpcUaClientProtocol, SimpleNamespace()),
        _model(),
        cast(SubscriptionServicePort, port),
    )
    with pytest.raises(HTTPException) as exc_info:
        await service.create_subscription("client-1")
    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_register_monitored_items_success() -> None:
    port = _FakeSubscriptionPort()
    service = SubscriptionAppService(
        cast(OpcUaClientProtocol, SimpleNamespace()),
        _model(),
        cast(SubscriptionServicePort, port),
        request=_request(),
    )
    dto = await service.register_monitored_items("sub-1", "client-1", ["asset-root"], max_depth=2)
    assert dto == {"subscriptionId": "sub-1", "monitoredItems": ["asset-root"], "registered": 1}


@pytest.mark.asyncio
async def test_register_monitored_items_wraps_client_id_validation_error() -> None:
    port = _FakeSubscriptionPort()
    service = SubscriptionAppService(
        cast(OpcUaClientProtocol, SimpleNamespace()),
        _model(),
        cast(SubscriptionServicePort, port),
        request=_request(),
    )
    with pytest.raises(HTTPException) as exc_info:
        await service.register_monitored_items("sub-1", "", ["asset-root"])
    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_register_monitored_items_not_found_path_is_wrapped() -> None:
    port = _FakeSubscriptionPort()
    port.register_result = False
    service = SubscriptionAppService(
        cast(OpcUaClientProtocol, SimpleNamespace()),
        _model(),
        cast(SubscriptionServicePort, port),
    )
    with pytest.raises(HTTPException) as exc_info:
        await service.register_monitored_items("sub-missing", "client-1", ["asset-root"])
    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_get_pending_updates_success() -> None:
    port = _FakeSubscriptionPort()
    port.sync_result = SubscriptionSyncResult(
        updates=[
            SubscriptionUpdate(
                sequence_number=1,
                element_id="asset-root",
                node_id="ns=2;s=Root",
                value=42,
                quality="Good",
                timestamp="2026-01-01T00:00:00Z",
            )
        ],
        queue_overflow=False,
        dropped_from_sequence=None,
        dropped_to_sequence=None,
        stream_active=False,
    )
    service = SubscriptionAppService(
        cast(OpcUaClientProtocol, SimpleNamespace()),
        _model(),
        cast(SubscriptionServicePort, port),
    )
    dto = await service.get_pending_updates("sub-1", "client-1", acknowledge_sequence=0)
    assert dto["queueOverflow"] is False
    assert dto["updates"][0]["sequenceNumber"] == 1


@pytest.mark.asyncio
async def test_get_pending_updates_wraps_missing_client_error() -> None:
    port = _FakeSubscriptionPort()
    service = SubscriptionAppService(
        cast(OpcUaClientProtocol, SimpleNamespace()),
        _model(),
        cast(SubscriptionServicePort, port),
    )
    with pytest.raises(HTTPException) as exc_info:
        await service.get_pending_updates("sub-1", "")
    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_get_pending_updates_wraps_not_found_and_stream_active_errors() -> None:
    port_none = _FakeSubscriptionPort()
    port_none.sync_result = None
    service_none = SubscriptionAppService(
        cast(OpcUaClientProtocol, SimpleNamespace()),
        _model(),
        cast(SubscriptionServicePort, port_none),
    )
    with pytest.raises(HTTPException) as exc_none:
        await service_none.get_pending_updates("sub-missing", "client-1")
    assert exc_none.value.status_code == 502

    port_stream = _FakeSubscriptionPort()
    port_stream.sync_result = SubscriptionSyncResult(
        updates=[],
        queue_overflow=False,
        dropped_from_sequence=None,
        dropped_to_sequence=None,
        stream_active=True,
    )
    service_stream = SubscriptionAppService(
        cast(OpcUaClientProtocol, SimpleNamespace()),
        _model(),
        cast(SubscriptionServicePort, port_stream),
    )
    with pytest.raises(HTTPException) as exc_stream:
        await service_stream.get_pending_updates("sub-1", "client-1")
    assert exc_stream.value.status_code == 502


@pytest.mark.asyncio
async def test_delete_subscriptions_success_and_wrap_error() -> None:
    port = _FakeSubscriptionPort()
    port.delete_results = [
        SubscriptionDeleteResult(success=True, subscription_id="sub-1"),
        SubscriptionDeleteResult(success=False, subscription_id="sub-2", error={"code": 404, "message": "missing"}),
    ]
    service = SubscriptionAppService(
        cast(OpcUaClientProtocol, SimpleNamespace()),
        _model(),
        cast(SubscriptionServicePort, port),
    )
    dto = await service.delete_subscriptions(["sub-1", "sub-2"], client_id="client-1")
    assert dto == {"deleted": 1, "requested": 2}

    port.delete_error = RuntimeError("fail-delete")
    with pytest.raises(HTTPException) as exc_info:
        await service.delete_subscriptions(["sub-1"])
    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_list_subscriptions_success_and_wrap_error() -> None:
    port = _FakeSubscriptionPort()
    port.list_results = [
        SubscriptionDetail(
            subscription_id="sub-1",
            client_id="client-1",
            display_name="Demo",
            monitored_objects=[{"elementId": "asset-root"}],
            mode="poll",
        )
    ]
    service = SubscriptionAppService(
        cast(OpcUaClientProtocol, SimpleNamespace()),
        _model(),
        cast(SubscriptionServicePort, port),
    )
    dto = await service.list_subscriptions(client_id="client-1", subscription_ids=["sub-1"])
    assert dto["subscriptions"][0]["subscriptionId"] == "sub-1"

    port.list_error = RuntimeError("fail-list")
    with pytest.raises(HTTPException) as exc_info:
        await service.list_subscriptions()
    assert exc_info.value.status_code == 502
