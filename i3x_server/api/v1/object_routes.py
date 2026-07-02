from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from i3x_server.api.v1.common_helpers import _resolve_model_nodes
from i3x_server.api.v1.contracts import (
    BulkResponse,
    BulkResultItem,
    GetObjectsRequest,
    ObjectInstanceResponse,
    SuccessResponse,
    _bulk_response,
    _bulk_result_error,
    _bulk_result_success,
)
from i3x_server.api.v1.object_helpers import (
    _resolved_type_element_id_for_node,
    _to_object_instance,
)
from i3x_server.api.v1.objecttype_helpers import _get_object_endpoint_context
from i3x_server.application.ports.opcua import OpcUaClientProtocol
from i3x_server.bootstrap.dependencies import get_opcua_client, get_or_build_model
from i3x_server.schemas.state import BuildResult

router = APIRouter(prefix="/v1", tags=["v1"])


@router.get("/objects", response_model=SuccessResponse[list[ObjectInstanceResponse]])
async def get_objects_v1(
    request: Request,
    type_element_id: str | None = Query(default=None, alias="typeElementId"),
    include_metadata: bool = Query(default=False, alias="includeMetadata"),
    root: bool | None = Query(default=None),
    model: BuildResult = Depends(get_or_build_model),
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> SuccessResponse[list[ObjectInstanceResponse]]:
    (
        namespace_infos,
        object_type_element_ids_by_node_id,
        object_type_element_ids_by_source_type,
    ) = await _get_object_endpoint_context(request, model, opcua_client)

    if root is True:
        hierarchy_parent_ids = set()
        raw_hierarchy_parent_by_id = getattr(model, "hierarchy_parent_by_id", None)
        if isinstance(raw_hierarchy_parent_by_id, dict):
            hierarchy_parent_ids = {
                node_id for node_id in raw_hierarchy_parent_by_id.keys() if isinstance(node_id, str)
            }
        composition_parent_ids = set()
        raw_composition_parent_by_id = getattr(model, "composition_parent_by_id", None)
        if isinstance(raw_composition_parent_by_id, dict):
            composition_parent_ids = {
                node_id for node_id in raw_composition_parent_by_id.keys() if isinstance(node_id, str)
            }
        nodes = [
            node
            for node in model.nodes_by_id.values()
            if (
                node.id not in hierarchy_parent_ids
                and node.id not in composition_parent_ids
                and node.kind in {"asset", "eventSource"}
            )
        ]
        if not nodes:
            nodes = [model.nodes_by_id[node_id] for node_id in model.root_ids if node_id in model.nodes_by_id]
    else:
        nodes = list(model.nodes_by_id.values())
    if type_element_id is not None:
        nodes = [
            node
            for node in nodes
            if node.type == type_element_id
            or node.kind == type_element_id
            or _resolved_type_element_id_for_node(
                node,
                namespace_infos,
                object_type_element_ids_by_node_id,
                object_type_element_ids_by_source_type,
            )
            == type_element_id
        ]
    return SuccessResponse(
        result=[
            _to_object_instance(
                model,
                node,
                include_metadata,
                namespace_infos,
                object_type_element_ids_by_node_id,
                object_type_element_ids_by_source_type,
            )
            for node in nodes
        ]
    )


@router.post("/objects/list", response_model=BulkResponse[ObjectInstanceResponse])
async def list_objects_by_id_v1(
    request: Request,
    body: GetObjectsRequest,
    model: BuildResult = Depends(get_or_build_model),
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> BulkResponse[ObjectInstanceResponse]:
    (
        namespace_infos,
        object_type_element_ids_by_node_id,
        object_type_element_ids_by_source_type,
    ) = await _get_object_endpoint_context(request, model, opcua_client)

    results: list[BulkResultItem[ObjectInstanceResponse]] = []
    for element_id, node in _resolve_model_nodes(model, body.elementIds):
        if node is None:
            results.append(_bulk_result_error(element_id, "Object not found"))
            continue
        results.append(
            _bulk_result_success(
                element_id,
                _to_object_instance(
                    model,
                    node,
                    include_metadata=body.includeMetadata,
                    namespace_infos=namespace_infos,
                    object_type_element_ids_by_node_id=object_type_element_ids_by_node_id,
                    object_type_element_ids_by_source_type=object_type_element_ids_by_source_type,
                ),
            )
        )
    return _bulk_response(results)
