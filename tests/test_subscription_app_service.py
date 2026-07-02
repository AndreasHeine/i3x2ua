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
        self.unregister_result = True
        self.activate_returns: list[int | None] = [1]
        self.sync_result: SubscriptionSyncResult | None = None
        self.active_sequence: list[bool] = [False]
        self.wait_returns: list[list[SubscriptionUpdate] | None] = []
        self.deactivated: list[tuple[str, int]] = []
        self.delete_results: list[SubscriptionDeleteResult] = []
        self.list_results: list[SubscriptionDetail] = []
        self.create_error: Exception | None = None
        self.register_error: Exception | None = None
        self.unregister_error: Exception | None = None
        self.sync_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.list_error: Exception | None = None

    async def activate_stream(self, client_id: str | None, subscription_id: str) -> int | None:
        del client_id, subscription_id
        return self.activate_returns.pop(0) if self.activate_returns else None

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

    async def unregister_items(
        self,
        client_id: str | None,
        subscription_id: str,
        element_ids: list[str],
        model: BuildResult,
    ) -> bool:
        del client_id, subscription_id, element_ids, model
        if self.unregister_error:
            raise self.unregister_error
        return self.unregister_result

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
async def test_register_monitored_items_preserves_client_id_validation_error() -> None:
    port = _FakeSubscriptionPort()
    service = SubscriptionAppService(
        cast(OpcUaClientProtocol, SimpleNamespace()),
        _model(),
        cast(SubscriptionServicePort, port),
        request=_request(),
    )
    with pytest.raises(HTTPException) as exc_info:
        await service.register_monitored_items("sub-1", "", ["asset-root"])
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_register_monitored_items_not_found_path_is_preserved() -> None:
    port = _FakeSubscriptionPort()
    port.register_result = False
    service = SubscriptionAppService(
        cast(OpcUaClientProtocol, SimpleNamespace()),
        _model(),
        cast(SubscriptionServicePort, port),
    )
    with pytest.raises(HTTPException) as exc_info:
        await service.register_monitored_items("sub-missing", "client-1", ["asset-root"])
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_unregister_monitored_items_success_and_preserved_errors() -> None:
    port = _FakeSubscriptionPort()
    service = SubscriptionAppService(
        cast(OpcUaClientProtocol, SimpleNamespace()),
        _model(),
        cast(SubscriptionServicePort, port),
        request=_request(),
    )

    dto = await service.unregister_monitored_items("sub-1", "client-1", ["asset-root"])
    assert dto == {"subscriptionId": "sub-1", "monitoredItems": ["asset-root"], "registered": 1}

    with pytest.raises(HTTPException) as exc_client:
        await service.unregister_monitored_items("sub-1", "", ["asset-root"])
    assert exc_client.value.status_code == 400

    port.unregister_result = False
    with pytest.raises(HTTPException) as exc_not_found:
        await service.unregister_monitored_items("sub-missing", "client-1", ["asset-root"])
    assert exc_not_found.value.status_code == 404

    port.unregister_result = True
    port.unregister_error = RuntimeError("boom")
    with pytest.raises(HTTPException) as exc_wrapped:
        await service.unregister_monitored_items("sub-1", "client-1", ["asset-root"])
    assert exc_wrapped.value.status_code == 502


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
async def test_get_pending_updates_preserves_missing_client_error() -> None:
    port = _FakeSubscriptionPort()
    service = SubscriptionAppService(
        cast(OpcUaClientProtocol, SimpleNamespace()),
        _model(),
        cast(SubscriptionServicePort, port),
    )
    with pytest.raises(HTTPException) as exc_info:
        await service.get_pending_updates("sub-1", "")
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_get_pending_updates_preserves_not_found_and_stream_active_errors() -> None:
    port_none = _FakeSubscriptionPort()
    port_none.sync_result = None
    service_none = SubscriptionAppService(
        cast(OpcUaClientProtocol, SimpleNamespace()),
        _model(),
        cast(SubscriptionServicePort, port_none),
    )
    with pytest.raises(HTTPException) as exc_none:
        await service_none.get_pending_updates("sub-missing", "client-1")
    assert exc_none.value.status_code == 404

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
    assert exc_stream.value.status_code == 400


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
async def test_delete_subscription_items_success_and_wrap_error() -> None:
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
    items = await service.delete_subscription_items(["sub-1", "sub-2"], client_id="client-1")
    assert items[0]["success"] is True
    assert items[1]["success"] is False
    assert items[1]["error"] == {"code": 404, "message": "missing"}

    port.delete_error = RuntimeError("fail-delete")
    with pytest.raises(HTTPException) as exc_info:
        await service.delete_subscription_items(["sub-1"])
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


@pytest.mark.asyncio
async def test_list_subscription_items_with_missing_requested_id() -> None:
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
    items = await service.list_subscription_items(client_id="client-1", subscription_ids=["sub-1", "missing"])
    assert items[0]["success"] is True
    assert items[0]["result"] is not None
    assert items[1]["success"] is False
    assert items[1]["error"] == {"code": 404, "message": "Subscription not found: missing"}

    port.list_error = RuntimeError("fail-list")
    with pytest.raises(HTTPException) as exc_info:
        await service.list_subscription_items()
    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_begin_stream_session_success_and_relaxed_scope() -> None:
    port = _FakeSubscriptionPort()
    port.activate_returns = [None, 7]
    port.sync_result = SubscriptionSyncResult(updates=[])
    service = SubscriptionAppService(
        cast(OpcUaClientProtocol, SimpleNamespace()),
        _model(),
        cast(SubscriptionServicePort, port),
        request=_request(),
    )

    session = await service.begin_stream_session(
        subscription_id="sub-1",
        client_id="client-1",
        acknowledge_sequence=2,
    )

    assert session["clientId"] == "client-1"
    assert session["subscriptionId"] == "sub-1"
    assert session["streamGeneration"] == 7
    assert session["scopeRelaxed"] is True
    assert session["acknowledged"].updates == []


@pytest.mark.asyncio
async def test_begin_stream_session_preserves_not_found_and_validation_errors() -> None:
    port = _FakeSubscriptionPort()
    service = SubscriptionAppService(
        cast(OpcUaClientProtocol, SimpleNamespace()),
        _model(),
        cast(SubscriptionServicePort, port),
        request=_request(),
    )

    with pytest.raises(HTTPException) as exc_missing_client:
        await service.begin_stream_session("sub-1", "")
    assert exc_missing_client.value.status_code == 400

    port.activate_returns = [None, None]
    with pytest.raises(HTTPException) as exc_missing_subscription:
        await service.begin_stream_session("sub-missing", "client-1")
    assert exc_missing_subscription.value.status_code == 404


@pytest.mark.asyncio
async def test_begin_stream_session_wraps_unexpected_errors() -> None:
    port = _FakeSubscriptionPort()
    port.activate_returns = [1]
    port.sync_error = RuntimeError("boom")
    service = SubscriptionAppService(
        cast(OpcUaClientProtocol, SimpleNamespace()),
        _model(),
        cast(SubscriptionServicePort, port),
        request=_request(),
    )

    with pytest.raises(HTTPException) as exc_info:
        await service.begin_stream_session("sub-1", "client-1")
    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_iter_stream_events_emits_connected_keepalive_updates_close() -> None:
    port = _FakeSubscriptionPort()
    port.active_sequence = [True, True, False]
    port.wait_returns = [[], [SubscriptionUpdate(2, "asset-root", "ns=2;s=Root", 42, "Good", "2026-01-01T00:00:00Z")]]
    service = SubscriptionAppService(
        cast(OpcUaClientProtocol, SimpleNamespace()),
        _model(),
        cast(SubscriptionServicePort, port),
    )

    events = [
        event
        async for event in service.iter_stream_events(
            subscription_id="sub-1",
            client_id="client-1",
            stream_generation=3,
            acknowledged_updates=[],
            acknowledge_sequence=0,
            timeout_seconds=0.1,
        )
    ]

    kinds = [event["kind"] for event in events]
    assert kinds == ["connected", "keepalive", "updates", "close"]
    assert len(events[2]["updates"]) == 1
    assert port.deactivated == [("sub-1", 3)]
