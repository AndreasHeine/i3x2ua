from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from i3x_server.api.v1.contracts import (
    BulkResponse,
    GetRelationshipTypesRequest,
    Namespace,
    RelationshipType,
    ServerInfo,
    SuccessResponse,
    _bulk_response,
    _map_lookup_bulk_result_items,
)
from i3x_server.application.dependencies import get_model_query_service
from i3x_server.application.services.model_query import ModelQueryService

router = APIRouter(prefix="/v1", tags=["v1"])


@router.get("/info", response_model=SuccessResponse[ServerInfo])
async def get_info_v1(
    model_query_service: ModelQueryService = Depends(get_model_query_service),
) -> SuccessResponse[ServerInfo]:
    info = await model_query_service.get_server_info()
    return SuccessResponse(result=ServerInfo.model_validate(info.model_dump()))


@router.get("/namespaces", response_model=SuccessResponse[list[Namespace]])
async def get_namespaces_v1(
    model_query_service: ModelQueryService = Depends(get_model_query_service),
) -> SuccessResponse[list[Namespace]]:
    namespaces = await model_query_service.get_namespaces()
    payload = [Namespace(uri=item.uri, displayName=item.displayName) for item in namespaces]
    return SuccessResponse(result=payload)


@router.get("/relationshiptypes", response_model=SuccessResponse[list[RelationshipType]])
async def get_relationship_types_v1(
    namespace_uri: str | None = Query(default=None, alias="namespaceUri"),
    model_query_service: ModelQueryService = Depends(get_model_query_service),
) -> SuccessResponse[list[RelationshipType]]:
    items = await model_query_service.get_relationship_types(namespace_uri)
    return SuccessResponse(result=[RelationshipType.model_validate(item) for item in items])


@router.post("/relationshiptypes/query", response_model=BulkResponse[RelationshipType])
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
