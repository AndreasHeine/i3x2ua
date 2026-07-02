from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from i3x_server.api.v1.contracts import (
    ModelMetricsResponse,
    ModelNamespaceGapItem,
    ModelNamespaceGapResponse,
    SuccessResponse,
)
from i3x_server.api.v1.monolithic import _build_model_metrics, _namespace_index_from_node_id
from i3x_server.application.dependencies import get_or_build_model
from i3x_server.schemas.state import BuildResult

router = APIRouter(prefix="/v1", tags=["v1"])


@router.get("/model/metrics", response_model=SuccessResponse[ModelMetricsResponse])
async def get_model_metrics_v1(
    model: BuildResult = Depends(get_or_build_model),
) -> SuccessResponse[ModelMetricsResponse]:
    return SuccessResponse(result=_build_model_metrics(model))


@router.get("/model/namespace-gaps", response_model=SuccessResponse[ModelNamespaceGapResponse])
async def get_model_namespace_gaps_v1(
    limit: int = Query(default=50, ge=1, le=500),
    model: BuildResult = Depends(get_or_build_model),
) -> SuccessResponse[ModelNamespaceGapResponse]:
    missing: list[ModelNamespaceGapItem] = []
    by_kind: dict[str, int] = {}
    by_namespace_index: dict[str, int] = {}
    for node_id, node in model.nodes_by_id.items():
        namespace_uri = model.namespace_uri_by_id.get(node_id)
        if isinstance(namespace_uri, str) and namespace_uri.strip():
            continue
        namespace_index = _namespace_index_from_node_id(node.source_node_id)
        kind = str(node.kind)
        by_kind[kind] = by_kind.get(kind, 0) + 1
        namespace_key = str(namespace_index) if namespace_index is not None else "unknown"
        by_namespace_index[namespace_key] = by_namespace_index.get(namespace_key, 0) + 1
        source_node_id = node.source_node_id
        missing.append(
            ModelNamespaceGapItem(
                elementId=node.id,
                displayName=node.name,
                kind=node.kind,
                sourceNodeId=source_node_id,
                sourceNamespaceIndex=namespace_index,
            )
        )

    missing.sort(key=lambda item: (item.kind, item.displayName, item.elementId))
    return SuccessResponse(
        result=ModelNamespaceGapResponse(
            totalMissing=len(missing),
            byKind=by_kind,
            byNamespaceIndex=by_namespace_index,
            items=missing[:limit],
        )
    )
