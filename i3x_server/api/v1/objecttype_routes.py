from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from i3x_server.api.v1.common_helpers import _raise_opcua_error
from i3x_server.api.v1.contracts import (
    BulkResponse,
    GetObjectTypesRequest,
    ObjectTypeResponse,
    SuccessResponse,
    _bulk_response,
    _map_lookup_bulk_result_items,
)
from i3x_server.api.v1.object_helpers import _canonical_namespace_uri
from i3x_server.api.v1.objecttype_helpers import _get_object_type_context
from i3x_server.application.ports.opcua import OpcUaClientProtocol
from i3x_server.bootstrap.dependencies import get_opcua_client, get_or_build_model
from i3x_server.schemas.state import BuildResult

router = APIRouter(prefix="/v1", tags=["v1"])


@router.get("/objecttypes", response_model=SuccessResponse[list[ObjectTypeResponse]])
async def get_object_types_v1(
    request: Request,
    namespace_uri: str | None = Query(default=None, alias="namespaceUri"),
    model: BuildResult = Depends(get_or_build_model),
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> SuccessResponse[list[ObjectTypeResponse]]:
    try:
        context = await _get_object_type_context(request, model, opcua_client)
    except Exception as exc:
        _raise_opcua_error("read object types", exc)
    namespace_infos = context.namespace_infos
    items = list(context.items)

    if namespace_uri:
        canonical_filter = _canonical_namespace_uri(namespace_uri, namespace_infos)
        items = [item for item in items if item.namespaceUri == canonical_filter]
    return SuccessResponse(result=items)


@router.post("/objecttypes/query", response_model=BulkResponse[ObjectTypeResponse])
async def query_object_types_v1(
    request: Request,
    body: GetObjectTypesRequest,
    model: BuildResult = Depends(get_or_build_model),
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> BulkResponse[ObjectTypeResponse]:
    try:
        context = await _get_object_type_context(request, model, opcua_client)
    except Exception as exc:
        _raise_opcua_error("query object types", exc)
    listed_types = context.items

    indexed = {item.elementId: item for item in listed_types}
    return _bulk_response(
        _map_lookup_bulk_result_items(
            body.elementIds,
            indexed,
            not_found_message="Object type not found",
        )
    )
