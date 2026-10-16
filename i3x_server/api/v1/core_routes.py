from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from i3x_server.api.openapi import CONDITIONAL_GET_OPENAPI
from i3x_server.api.v1.contracts import (
    BulkResponse,
    GetRelationshipTypesRequest,
    HttpErrorResponse,
    Namespace,
    RelationshipType,
    ServerInfo,
    SuccessResponse,
    _bulk_response,
    _map_lookup_bulk_result_items,
)
from i3x_server.application.dependencies import get_model_query_service
from i3x_server.application.services.model_query import ModelQueryService
from i3x_server.http_caching import NAMESPACES_PART, content_revision, enable_etag, record_revision

router = APIRouter(prefix="/v1")


@router.get(
    "/info",
    response_model=SuccessResponse[ServerInfo],
    summary="Get server info",
    description=("Return i3X server metadata including supported capabilities, server version, and spec version."),
    tags=["Info"],
)
async def get_info_v1(
    model_query_service: ModelQueryService = Depends(get_model_query_service),
) -> SuccessResponse[ServerInfo]:
    info = await model_query_service.get_server_info()
    return SuccessResponse(success=True, result=ServerInfo.model_validate(info.model_dump()))


@router.get(
    "/namespaces",
    response_model=SuccessResponse[list[Namespace]],
    responses={502: {"model": HttpErrorResponse, "description": "Failed to read upstream OPC UA namespaces."}},
    summary="List namespaces",
    description="Return all namespaces available in the current model. Supports conditional GET via ETag.",
    dependencies=[Depends(enable_etag)],
    openapi_extra=CONDITIONAL_GET_OPENAPI,
    tags=["Exploratory"],
)
async def get_namespaces_v1(
    request: Request,
    model_query_service: ModelQueryService = Depends(get_model_query_service),
) -> SuccessResponse[list[Namespace]]:
    namespaces = await model_query_service.get_namespaces()
    payload = [Namespace(uri=item.uri, displayName=item.displayName) for item in namespaces]
    record_revision(request, NAMESPACES_PART, content_revision((item.uri, item.displayName) for item in payload))
    return SuccessResponse(success=True, result=payload)


@router.get(
    "/relationshiptypes",
    response_model=SuccessResponse[list[RelationshipType]],
    summary="List relationship types",
    description=(
        "Return the built-in i3X relationships and graph relationship types discovered in the mapped model, "
        "not all OPC UA ReferenceTypes. Optionally filter by exact namespace URI. "
        "Graph types retain their source namespace; colliding names use namespace-qualified IDs. "
        "Supports conditional GET via ETag."
    ),
    dependencies=[Depends(enable_etag)],
    openapi_extra=CONDITIONAL_GET_OPENAPI,
    tags=["Exploratory"],
)
async def get_relationship_types_v1(
    namespace_uri: str | None = Query(default=None, alias="namespaceUri"),
    model_query_service: ModelQueryService = Depends(get_model_query_service),
) -> SuccessResponse[list[RelationshipType]]:
    items = await model_query_service.get_relationship_types(namespace_uri)
    return SuccessResponse(success=True, result=[RelationshipType.model_validate(item) for item in items])


@router.post(
    "/relationshiptypes/query",
    response_model=BulkResponse[RelationshipType],
    summary="Query relationship types by ID",
    description=(
        "Resolve one or more relationship type element IDs using a bulk request. "
        "Unknown IDs return per-item not-found errors."
    ),
    tags=["Exploratory"],
)
async def query_relationship_types_v1(
    body: GetRelationshipTypesRequest,
    model_query_service: ModelQueryService = Depends(get_model_query_service),
) -> BulkResponse[RelationshipType]:
    items = {
        item["elementId"]: RelationshipType.model_validate(item)
        for item in await model_query_service.get_relationship_types()
    }
    return _bulk_response(
        _map_lookup_bulk_result_items(
            body.elementIds,
            items,
            not_found_message="Relationship type not found",
        )
    )
