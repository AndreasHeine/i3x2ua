from __future__ import annotations

from time import perf_counter
from typing import Any

from fastapi import APIRouter, Depends, Request

from i3x_server.api.v1.common_helpers import (
    _good_no_data_vqt,
    _raise_not_found,
    _raise_opcua_error,
    _resolve_model_nodes,
)
from i3x_server.api.v1.contracts import (
    VQT,
    BulkResponse,
    BulkResultItem,
    CurrentValueResult,
    GetObjectHistoryRequest,
    GetObjectValueRequest,
    GetRelatedObjectsRequest,
    HistoricalValueResult,
    RelatedObjectResult,
    SuccessResponse,
    UpdateObjectValueRequest,
    UpdateObjectValuesRequest,
    _bulk_response,
    _bulk_result_error,
    _bulk_result_success,
)
from i3x_server.api.v1.monolithic import (
    _build_historical_value_result,
    _collect_history_lookup_and_node_ids,
    _collect_value_component_nodes,
    _not_implemented,
    _parse_history_time_range,
    _raise_invalid_argument,
    _raise_write_error,
    _vqt_from_data_value,
    _write_object_value_by_element_id,
    _writes_enabled,
    logger,
)
from i3x_server.api.v1.object_helpers import (
    _build_related_objects_for_node,
    _composition_children_for_node,
    _find_model_node,
)
from i3x_server.api.v1.objecttype_helpers import _get_object_endpoint_context
from i3x_server.application.ports.opcua import OpcUaClientProtocol
from i3x_server.bootstrap.dependencies import get_opcua_client, get_or_build_model
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult

router = APIRouter(prefix="/v1", tags=["v1"])


@router.post(
    "/objects/related",
    response_model=BulkResponse[list[RelatedObjectResult]],
    summary="Query related objects",
    description=(
        "Return all objects related to the requested elements across all relationship planes: "
        "hierarchy (`HasChildren`, `HasParent`), composition (`HasComponent`, `ComponentOf`), "
        "and graph (custom/non-hierarchical). "
        "Use `relationshipType` to filter results to a specific named relationship. "
        "Failing items include an item-level `responseDetail` alongside `error`."
    ),
)
async def query_related_objects_v1(
    request: Request,
    body: GetRelatedObjectsRequest,
    model: BuildResult = Depends(get_or_build_model),
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> BulkResponse[list[RelatedObjectResult]]:
    (
        namespace_infos,
        object_type_element_ids_by_node_id,
        object_type_element_ids_by_source_type,
    ) = await _get_object_endpoint_context(request, model, opcua_client)

    results: list[BulkResultItem[list[RelatedObjectResult]]] = []
    for element_id, node in _resolve_model_nodes(model, body.elementIds):
        if node is None:
            results.append(_bulk_result_error(element_id, "Object not found"))
            continue
        related = _build_related_objects_for_node(
            model,
            node,
            body.relationshipType,
            body.includeMetadata,
            namespace_infos,
            object_type_element_ids_by_node_id,
            object_type_element_ids_by_source_type,
        )
        results.append(_bulk_result_success(element_id, related))
    return _bulk_response(results)


@router.post(
    "/objects/value",
    response_model=BulkResponse[CurrentValueResult],
    summary="Query last known values",
    description=(
        "Return the last known value, quality, and timestamp for one or more objects. "
        "Quality and timestamp are sourced directly from the OPC UA server; quality values are "
        "`Good`, `Uncertain`, or `Bad`. "
        "A null value with `Good` or `Uncertain` quality is normalized to `GoodNoData`. "
        "When `maxDepth > 1`, component values are recursed using the **composition** adjacency only ÔÇö "
        "hierarchy-only children are never included. "
        "Failing items include an item-level `responseDetail` alongside `error`."
    ),
)
async def query_last_known_values_v1(
    body: GetObjectValueRequest,
    model: BuildResult = Depends(get_or_build_model),
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> BulkResponse[CurrentValueResult]:
    resolved_nodes = _resolve_model_nodes(model, body.elementIds)
    node_ids: list[str] = []
    component_nodes_by_element_id: dict[str, list[ModelNode]] = {}
    requested_depth = body.maxDepth if body.maxDepth is not None else 1
    for element_id, node in resolved_nodes:
        if node is None:
            continue
        if node.kind == "property":
            node_ids.append(node.source_node_id)
        component_nodes = _collect_value_component_nodes(model, node, requested_depth)
        component_nodes_by_element_id[element_id] = component_nodes
        node_ids.extend(item.source_node_id for item in component_nodes)

    values_by_node_id: dict[str, Any] = {}
    if node_ids:
        try:
            raw_data_values = await opcua_client.read_data_values(node_ids)
            values_by_node_id = {node_id: dv for node_id, dv in zip(node_ids, raw_data_values, strict=False)}
        except Exception as exc:
            _raise_opcua_error("read values", exc)

    results: list[BulkResultItem[CurrentValueResult]] = []
    for element_id, node in resolved_nodes:
        if node is None:
            results.append(_bulk_result_error(element_id, f"Element not found: {element_id}"))
            continue

        root_vqt = (
            _vqt_from_data_value(values_by_node_id[node.source_node_id])
            if node.source_node_id in values_by_node_id
            else _good_no_data_vqt()
        )
        component_nodes = component_nodes_by_element_id.get(element_id, [])
        components: dict[str, VQT] = {}
        for component_node in component_nodes:
            comp_dv = values_by_node_id.get(component_node.source_node_id)
            components[component_node.id] = (
                _vqt_from_data_value(comp_dv) if comp_dv is not None else _good_no_data_vqt()
            )

        result = CurrentValueResult(
            isComposition=bool(_composition_children_for_node(model, node)),
            value=root_vqt.value,
            quality=root_vqt.quality,
            timestamp=root_vqt.timestamp,
            components=components or None,
        )
        results.append(_bulk_result_success(element_id, result))
    return _bulk_response(results)


@router.post(
    "/objects/history",
    response_model=BulkResponse[HistoricalValueResult],
    summary="Query historical values",
    description=(
        "Return historical values for one or more objects within the specified time range. "
        "Values are ordered by source timestamp ascending. "
        "Component recursion follows the **composition** adjacency only ÔÇö hierarchy-only children are excluded. "
        "Failing items include an item-level `responseDetail` alongside `error`."
    ),
)
async def query_historical_values_v1(
    body: GetObjectHistoryRequest,
    model: BuildResult = Depends(get_or_build_model),
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> BulkResponse[HistoricalValueResult]:
    started = perf_counter()
    resolved_nodes = _resolve_model_nodes(model, body.elementIds)
    start_time, end_time = _parse_history_time_range(body)
    max_depth = body.maxDepth if body.maxDepth is not None else 1

    lookup, node_ids = _collect_history_lookup_and_node_ids(model, resolved_nodes, max_depth)

    values_by_node_id: dict[str, list[Any]] = {}
    unique_node_ids: list[str] = []
    if node_ids:
        unique_node_ids = list(dict.fromkeys(node_ids))
        try:
            values_by_node_id = await opcua_client.read_history_values(
                node_ids=unique_node_ids,
                start_time=start_time,
                end_time=end_time,
            )
        except Exception as exc:
            _raise_opcua_error("read historical values", exc)

    lookup_by_element_id = {element_id: (node, source_nodes) for element_id, node, source_nodes in lookup}

    results: list[BulkResultItem[HistoricalValueResult]] = []
    for element_id in body.elementIds:
        match = lookup_by_element_id.get(element_id)
        if match is None:
            results.append(_bulk_result_error(element_id, "Object not found"))
            continue

        node, source_nodes = match
        if not source_nodes:
            results.append(_bulk_result_error(element_id, "Object history not found"))
            continue

        result = _build_historical_value_result(model, node, source_nodes, values_by_node_id)
        results.append(_bulk_result_success(element_id, result))

    logger.info(
        "OPC UA history query finished requested_elements=%d resolved_nodes=%d values=%d success_items=%d "
        "error_items=%d duration_s=%.3f",
        len(body.elementIds),
        len(unique_node_ids),
        sum(len(item) for item in values_by_node_id.values()),
        sum(1 for item in results if item.success),
        sum(1 for item in results if not item.success),
        perf_counter() - started,
    )

    return _bulk_response(results)


@router.put(
    "/objects/value",
    response_model=BulkResponse[None],
    summary="Update current values",
    description=(
        "Write current values for one or more objects using a bulk request. "
        "Each item returns success or an item-level error, preserving request order."
    ),
)
async def update_object_values_v1(
    body: UpdateObjectValuesRequest,
    model: BuildResult = Depends(get_or_build_model),
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> BulkResponse[None]:
    if not _writes_enabled():
        _not_implemented("Current value updates")

    results: list[BulkResultItem[None]] = []
    for update in body.updates:
        ok, status_code, message, _diagnostics = await _write_object_value_by_element_id(
            model=model,
            opcua_client=opcua_client,
            element_id=update.elementId,
            payload_value=update.value,
        )
        if ok:
            results.append(_bulk_result_success(update.elementId, None))
        else:
            results.append(_bulk_result_error(update.elementId, message, code=status_code))

    return _bulk_response(results)


@router.get("/objects/{element_id}/history")
async def get_historical_values_v1(element_id: str) -> None:
    _not_implemented(f"Historical values for '{element_id}'")


@router.put("/objects/{element_id}/history")
async def update_object_history_v1(element_id: str) -> None:
    _not_implemented(f"Historical value updates for '{element_id}'")


@router.put("/objects/{element_id}/value", response_model=SuccessResponse[None])
async def update_object_value_v1(
    element_id: str,
    request: Request,
    body: UpdateObjectValueRequest | None = None,
    model: BuildResult = Depends(get_or_build_model),
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> SuccessResponse[None]:
    if not _writes_enabled():
        _not_implemented(f"Value update for '{element_id}'")

    if body is None:
        _raise_invalid_argument("body", None, "Missing request body")

    node = _find_model_node(model, element_id)
    if node is None:
        _raise_not_found("Object", element_id)
    if node.kind != "property":
        _raise_write_error(400, "bad_type_or_range")

    target_node_id = node.source_node_id
    principal = request.headers.get("x-principal") or "anonymous"
    started = perf_counter()

    ok, status_code, error_class, diagnostics = await _write_object_value_by_element_id(
        model=model,
        opcua_client=opcua_client,
        element_id=element_id,
        payload_value=body.value,
    )
    if not ok:
        logger.warning(
            (
                "Write audit principal=%s element_id=%s node_id=%s decision=deny class=%s "
                "variant_type=%s value_type=%s value_preview=%s error=%s duration_s=%.3f"
            ),
            principal,
            element_id,
            target_node_id,
            error_class,
            diagnostics.get("resolvedVariantType"),
            diagnostics.get("requestedValueType"),
            diagnostics.get("requestedValuePreview"),
            diagnostics.get("exception"),
            perf_counter() - started,
        )
        _raise_write_error(status_code, error_class)

    logger.info(
        "Write audit principal=%s element_id=%s node_id=%s decision=allow class=ok duration_s=%.3f",
        principal,
        element_id,
        target_node_id,
        perf_counter() - started,
    )
    return SuccessResponse(result=None)
