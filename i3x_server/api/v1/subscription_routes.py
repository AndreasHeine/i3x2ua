from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse

from i3x_server.api.v1.common_helpers import (
    _expand_subscription_bulk_item_element_ids,
    _fetch_namespace_infos,
    _map_delete_subscription_bulk_result_items,
    _map_subscription_detail_bulk_result_items,
    _require_client_id,
    _to_json_safe_value,
    _validate_subscription_element_ids,
)
from i3x_server.api.v1.contracts import (
    BulkResponse,
    CreateSubscriptionRequest,
    CreateSubscriptionResponse,
    DeleteSubscriptionsRequest,
    ListSubscriptionsRequest,
    RegisterMonitoredItemsRequest,
    StreamRequest,
    SubscriptionDetail,
    SuccessResponse,
    SyncBatch,
    SyncRequest,
    SyncUpdate,
    _bulk_response,
)
from i3x_server.api.v1.monolithic import _stream_debug_enabled, logger
from i3x_server.api.v1.object_helpers import _expanded_node_id
from i3x_server.application.dependencies import get_subscription_app_service
from i3x_server.application.ports.opcua import OpcUaClientProtocol
from i3x_server.application.services.subscription import SubscriptionAppService
from i3x_server.application.services.subscription_mapper import map_public_subscription_updates
from i3x_server.bootstrap.dependencies import get_opcua_client, get_or_build_model
from i3x_server.schemas.state import BuildResult

router = APIRouter(prefix="/v1", tags=["v1"])


@router.post("/subscriptions")
async def create_subscription_v1(
    body: CreateSubscriptionRequest,
    subscription_app_service: SubscriptionAppService = Depends(get_subscription_app_service),
) -> SuccessResponse[CreateSubscriptionResponse]:
    created = await subscription_app_service.create_subscription(
        client_id=body.clientId,
        display_name=body.displayName,
    )
    return SuccessResponse(
        result=CreateSubscriptionResponse(
            subscriptionId=created["subscriptionId"],
            clientId=created["clientId"],
            displayName=created["displayName"],
        )
    )


@router.post("/subscriptions/register")
async def register_monitored_items_v1(
    body: RegisterMonitoredItemsRequest,
    model: BuildResult = Depends(get_or_build_model),
    subscription_app_service: SubscriptionAppService = Depends(get_subscription_app_service),
) -> BulkResponse[None]:
    max_depth = 0 if body.maxDepth is None else body.maxDepth
    known_ids, results = _validate_subscription_element_ids(model, body.elementIds)

    await subscription_app_service.register_monitored_items(
        subscription_id=body.subscriptionId,
        client_id=body.clientId,
        element_ids=known_ids,
        max_depth=max_depth,
    )
    return _bulk_response(results)


@router.post("/subscriptions/unregister")
async def remove_monitored_items_v1(
    body: RegisterMonitoredItemsRequest,
    model: BuildResult = Depends(get_or_build_model),
    subscription_app_service: SubscriptionAppService = Depends(get_subscription_app_service),
) -> BulkResponse[None]:
    known_ids, results = _validate_subscription_element_ids(model, body.elementIds)

    await subscription_app_service.unregister_monitored_items(
        subscription_id=body.subscriptionId,
        client_id=body.clientId,
        element_ids=known_ids,
    )
    return _bulk_response(results)


@router.post(
    "/subscriptions/stream",
    summary="Stream subscription updates via SSE",
    description=(
        "Open a Server-Sent Events (SSE) stream for a subscription. "
        "Each `data:` event carries a JSON array of updates with `sequenceNumber`, "
        "`elementId`, `value`, `quality`, and `timestamp`. "
        "A `: connected` comment is sent immediately on connection. "
        "A `: keepalive` comment is sent periodically when there are no new updates. "
        "An `event: close` message is sent when the stream is terminated server-side. "
        "While a stream is active, `POST /subscriptions/sync` will return HTTP 400 for the same subscription. "
        "Opening a new stream for the same subscription closes the prior stream generation."
    ),
)
async def stream_subscription_v1(
    body: StreamRequest,
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
    subscription_app_service: SubscriptionAppService = Depends(get_subscription_app_service),
) -> StreamingResponse:
    namespace_infos = await _fetch_namespace_infos(opcua_client)

    stream_session = await subscription_app_service.begin_stream_session(
        subscription_id=body.subscriptionId,
        client_id=body.clientId,
        acknowledge_sequence=body.acknowledgeSequence,
    )
    client_id = stream_session["clientId"]
    stream_generation = stream_session["streamGeneration"]
    scope_relaxed = stream_session["scopeRelaxed"]
    acknowledged = stream_session["acknowledged"]

    if _stream_debug_enabled():
        logger.info(
            "Subscription stream open request client_id=%s subscription_id=%s acknowledge_sequence=%s",
            client_id,
            body.subscriptionId,
            body.acknowledgeSequence,
        )
        if scope_relaxed:
            logger.warning(
                "Stream activation recovered with relaxed client scope client_id=%s subscription_id=%s",
                client_id,
                body.subscriptionId,
            )
        logger.info(
            "Subscription stream activated client_id=%s subscription_id=%s generation=%s initial_updates=%s",
            client_id,
            body.subscriptionId,
            stream_generation,
            len(acknowledged.updates),
        )

    async def event_stream() -> Any:
        async for event in subscription_app_service.iter_stream_events(
            subscription_id=body.subscriptionId,
            client_id=client_id,
            stream_generation=stream_generation,
            acknowledged_updates=acknowledged.updates,
            acknowledge_sequence=body.acknowledgeSequence,
            timeout_seconds=2,
        ):
            kind = event["kind"]
            if kind == "connected":
                yield ": connected\n\n"
                continue
            if kind == "keepalive":
                if _stream_debug_enabled():
                    logger.info(
                        "Subscription stream keepalive subscription_id=%s generation=%s",
                        body.subscriptionId,
                        stream_generation,
                    )
                yield ": keepalive\n\n"
                continue
            if kind == "close":
                if _stream_debug_enabled():
                    logger.info(
                        "Subscription stream closing subscription_id=%s generation=%s",
                        body.subscriptionId,
                        stream_generation,
                    )
                yield "event: close\ndata: {}\n\n"
                continue

            updates = event["updates"]
            payload = map_public_subscription_updates(
                updates,
                element_id_mapper=lambda element_id: _expanded_node_id(element_id, namespace_infos),
                value_mapper=_to_json_safe_value,
            )
            payload = [
                {
                    "sequenceNumber": item["sequenceNumber"],
                    "elementId": item["elementId"],
                    "value": item["value"],
                    "quality": item["quality"],
                    "timestamp": item["timestamp"],
                }
                for item in payload
            ]
            encoded_payload = jsonable_encoder(payload)
            if _stream_debug_enabled():
                first_item = encoded_payload[0] if encoded_payload else {}
                logger.info(
                    (
                        "Subscription stream emit subscription_id=%s generation=%s updates=%s "
                        "first_element_id=%s first_keys=%s"
                    ),
                    body.subscriptionId,
                    stream_generation,
                    len(updates),
                    first_item.get("elementId"),
                    sorted(first_item.keys()) if isinstance(first_item, dict) else None,
                )
            yield f"data: {json.dumps(encoded_payload)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Content-Encoding": "identity",
        },
    )


@router.post(
    "/subscriptions/sync",
    summary="Sync subscription updates",
    description=(
        "Return all pending updates for a subscription and acknowledge previously received ones. "
        "Set `acknowledgeSequence` to the last sequence number the client processed to discard "
        "older entries from the queue. "
        "Pass `acknowledgeSequence=-1` to acknowledge and discard **all** pending updates. "
        "Returns HTTP 206 with a `responseDetail` if updates were dropped due to queue overflow since the last sync. "
        "Returns HTTP 400 if the subscription has an active SSE stream - close the stream before calling sync."
    ),
)
async def sync_subscription_v1(
    body: SyncRequest,
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
    subscription_app_service: SubscriptionAppService = Depends(get_subscription_app_service),
) -> Any:
    namespace_infos = await _fetch_namespace_infos(opcua_client)

    synced = await subscription_app_service.get_pending_updates(
        subscription_id=body.subscriptionId,
        client_id=body.clientId,
        acknowledge_sequence=body.acknowledgeSequence,
    )

    public_updates = [
        SyncUpdate(
            sequenceNumber=item["sequenceNumber"],
            elementId=_expanded_node_id(item["elementId"], namespace_infos),
            value=_to_json_safe_value(item["value"]),
            quality=item["quality"],
            timestamp=item["timestamp"],
        )
        for item in synced["updates"]
    ]
    batch_payload = []
    if public_updates:
        batch_payload = [
            SyncBatch(
                sequenceNumber=public_updates[-1].sequenceNumber,
                updates=public_updates,
            )
        ]
    result_payload = [item.model_dump(mode="json") for item in batch_payload]

    if synced["queueOverflow"]:
        detail = (
            "Updates were dropped from the subscription queue. "
            f"Dropped sequence numbers {synced['droppedFromSequence']} through {synced['droppedToSequence']}."
        )
        return JSONResponse(
            status_code=206,
            content={
                "success": True,
                "result": result_payload,
                "responseDetail": {
                    "title": "Updates dropped due to queue overflow",
                    "status": 206,
                    "detail": detail,
                },
            },
        )

    return SuccessResponse(result=result_payload)


@router.post("/subscriptions/delete")
async def delete_subscriptions_v1(
    body: DeleteSubscriptionsRequest,
    subscription_app_service: SubscriptionAppService = Depends(get_subscription_app_service),
) -> BulkResponse[None]:
    client_id = _require_client_id(body.clientId, "/subscriptions/delete")
    items = await subscription_app_service.delete_subscription_items(body.subscriptionIds, client_id=client_id)
    return _bulk_response(_map_delete_subscription_bulk_result_items(items))


@router.post("/subscriptions/list")
async def list_subscriptions_v1(
    body: ListSubscriptionsRequest,
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
    subscription_app_service: SubscriptionAppService = Depends(get_subscription_app_service),
) -> BulkResponse[SubscriptionDetail]:
    client_id = _require_client_id(body.clientId, "/subscriptions/list")
    namespace_infos = await _fetch_namespace_infos(opcua_client)

    items = await subscription_app_service.list_subscription_items(
        client_id=client_id,
        subscription_ids=body.subscriptionIds or None,
    )
    expanded_items = _expand_subscription_bulk_item_element_ids(items, namespace_infos)
    return _bulk_response(_map_subscription_detail_bulk_result_items(expanded_items))
