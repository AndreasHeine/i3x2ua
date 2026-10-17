"""MCP workflow adapters reusing the canonical i3X handlers and services."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from i3x_server.api.ua import get_ua_connection, get_ua_status
from i3x_server.api.v1.contracts import (
    CreateSubscriptionRequest,
    DeleteSubscriptionsRequest,
    GetObjectsRequest,
    GetObjectTypesRequest,
    GetObjectValueRequest,
    RegisterMonitoredItemsRequest,
)
from i3x_server.api.v1.monolithic import _to_vqt_from_history_value
from i3x_server.api.v1.object_routes import list_objects_by_id_v1
from i3x_server.api.v1.object_value_routes import query_last_known_values_v1
from i3x_server.api.v1.objecttype_routes import query_object_types_v1
from i3x_server.api.v1.subscription_routes import (
    create_subscription_v1,
    delete_subscriptions_v1,
    register_monitored_items_v1,
)
from i3x_server.application.dependencies import (
    get_model_query_service,
    get_opcua_client,
    get_or_build_model,
    get_subscription_app_service,
    get_subscription_service,
)
from i3x_server.application.services.asset_context import (
    AssetContextRequest,
    AssetContextService,
    AssetMonitoringRequest,
    AssetSearchRequest,
    AssetSnapshotRequest,
    DataAvailabilityRequest,
    SignalHistoryRequest,
    WorkflowOptions,
)
from i3x_server.domain.ports.opcua import BoundedHistoryReader

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/mcp/composites", tags=["MCP Helpers"])


async def _run(request: Request, options: WorkflowOptions, name: str) -> dict[str, Any]:
    started = perf_counter()
    collection_started = datetime.now(timezone.utc).isoformat()
    try:
        model = await asyncio.wait_for(get_or_build_model(request), timeout=options.timeoutSeconds)
    except TimeoutError:
        logger.warning("Composite model resolution timed out name=%s", name)
        return {
            "success": False,
            "errors": [{"stage": "model", "message": "Model resolution timed out"}],
            "collectionStartedAt": collection_started,
            "collectionCompletedAt": datetime.now(timezone.utc).isoformat(),
            "atomicSnapshot": False,
            "modelRevision": None,
        }
    opcua = get_opcua_client(request)

    async def query(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if operation == "objects":
                objects = await list_objects_by_id_v1(
                    request, GetObjectsRequest(**arguments, includeMetadata=True), model, opcua
                )
                return objects.model_dump(mode="json", by_alias=True)
            if operation == "types":
                types = await query_object_types_v1(request, GetObjectTypesRequest(**arguments), model, opcua)
                return types.model_dump(mode="json", by_alias=True)
            if operation == "values":
                values = await query_last_known_values_v1(GetObjectValueRequest(**arguments, maxDepth=1), model, opcua)
                return values.model_dump(mode="json")
            if operation == "connection":
                return (await get_ua_connection(opcua)).model_dump(mode="json")
            if operation == "status":
                return (await get_ua_status(opcua)).model_dump(mode="json")
            if operation == "capabilities":
                service = await get_model_query_service(model, opcua)
                return {"success": True, "result": (await service.get_server_info()).model_dump()}
            if operation == "history":
                if not isinstance(opcua, BoundedHistoryReader):
                    return {"success": False, "error": "Bounded history is unsupported; unbounded fallback is disabled"}
                node = model.nodes_by_id[arguments["elementId"]]
                try:
                    history = await opcua.read_bounded_history(
                        node.source_node_id, arguments["startTime"], arguments["endTime"], arguments["maxPoints"]
                    )
                except Exception as exc:
                    logger.exception("Bounded history read failed element_id=%s", node.id)
                    return {"success": False, "error": str(exc)}
                values_list = []
                for value in history.values:
                    vqt = _to_vqt_from_history_value(value).model_dump(mode="json")
                    if value.SourceTimestamp is not None:
                        vqt["timestampKind"] = "source"
                    elif value.ServerTimestamp is not None:
                        vqt["timestampKind"] = "server"
                    else:
                        vqt["timestamp"] = None
                        vqt["timestampKind"] = "unavailable"
                    values_list.append(vqt)
                values_list.sort(key=lambda value: value["timestamp"] or "")
                return {"success": True, "values": values_list, "truncated": history.truncated}
            subscription_service = await get_subscription_app_service(
                request, model, opcua, get_subscription_service(request)
            )
            if operation == "create":
                created = await create_subscription_v1(CreateSubscriptionRequest(**arguments), subscription_service)
                return created.model_dump(mode="json")
            if operation == "register":
                registered = await register_monitored_items_v1(
                    RegisterMonitoredItemsRequest(**arguments), model, subscription_service
                )
                return registered.model_dump(mode="json")
            if operation == "delete":
                deleted = await delete_subscriptions_v1(DeleteSubscriptionsRequest(**arguments), subscription_service)
                return deleted.model_dump(mode="json")
            raise ValueError(f"Unknown composite operation {operation}")
        except HTTPException as exc:
            logger.warning("Composite operation failed operation=%s detail=%s", operation, exc.detail)
            return {"success": False, "statusCode": exc.status_code, "error": exc.detail}

    remaining = options.timeoutSeconds - (perf_counter() - started)
    if remaining <= 0:
        return {
            "success": False,
            "errors": [{"stage": "model", "message": "Workflow deadline exceeded"}],
            "collectionStartedAt": collection_started,
            "collectionCompletedAt": datetime.now(timezone.utc).isoformat(),
            "atomicSnapshot": False,
            "modelRevision": model.revision,
        }
    result = await AssetContextService(model, query).run(name, options.model_copy(update={"timeoutSeconds": remaining}))
    result["collectionStartedAt"] = collection_started
    return result


@router.post("/discover", operation_id="discoverAssetContext", summary="Discover bounded asset context")
async def discover_asset_context(request: Request, body: AssetContextRequest) -> dict[str, Any]:
    """Discover a bounded graph, metadata and distinct schemas; optionally read values. Never invent element IDs."""
    return await _run(request, body, "discoverAssetContext")


@router.post("/find", operation_id="findAssets", summary="Find explicit asset candidates")
async def find_assets(request: Request, body: AssetSearchRequest) -> dict[str, Any]:
    """Search mapped objects by name substring, exact type/namespace and optional bounded scope; never auto-select."""
    return await _run(request, body, "findAssets")


@router.post("/snapshot", operation_id="getAssetSnapshot", summary="Collect a non-atomic asset evidence bundle")
async def get_asset_snapshot(request: Request, body: AssetSnapshotRequest) -> dict[str, Any]:
    """Discover signals through hierarchy/composition and read their last known VQT. Not an atomic or health verdict."""
    return await _run(request, body, "getAssetSnapshot")


@router.post("/monitor", operation_id="startAssetMonitoring", summary="Start bounded asset monitoring with rollback")
async def start_asset_monitoring(request: Request, body: AssetMonitoringRequest) -> dict[str, Any]:
    """Register expanded signals with rollback on partial failure. clientId is not authentication."""
    return await _run(request, body, "startAssetMonitoring")


@router.post("/history", operation_id="summarizeSignalHistory", summary="Summarize bounded signal history")
async def summarize_signal_history(request: Request, body: SignalHistoryRequest) -> dict[str, Any]:
    """Summarize a bounded history page per signal, preserving quality coverage; no diagnosis or unbounded fallback."""
    return await _run(request, body, "summarizeSignalHistory")


@router.post("/diagnose", operation_id="diagnoseDataAvailability", summary="Collect data-availability evidence")
async def diagnose_data_availability(request: Request, body: DataAvailabilityRequest) -> dict[str, Any]:
    """Combine connection, capabilities, model and VQT evidence. Old timestamps alone do not prove disconnection."""
    return await _run(request, body, "diagnoseDataAvailability")
