from fastapi import APIRouter, Depends

from i3x_server.dependencies import get_or_build_model
from i3x_server.errors import i3x_http_error
from i3x_server.schemas.i3x import ModelChildrenResponse, ModelNode, ModelRootResponse
from i3x_server.schemas.state import BuildResult

router = APIRouter(prefix="/model", tags=["model"])


@router.get("", response_model=ModelRootResponse)
async def get_model(model: BuildResult = Depends(get_or_build_model)) -> ModelRootResponse:
    items = [model.nodes_by_id[node_id] for node_id in model.root_ids if node_id in model.nodes_by_id]
    return ModelRootResponse(items=items)


@router.get("/{model_id}", response_model=ModelNode)
async def get_model_by_id(model_id: str, model: BuildResult = Depends(get_or_build_model)) -> ModelNode:
    node = model.nodes_by_id.get(model_id)
    if node is None:
        raise i3x_http_error(404, "ModelNotFound", f"Model id '{model_id}' does not exist")
    return node


@router.get("/{model_id}/children", response_model=ModelChildrenResponse)
async def get_model_children(model_id: str, model: BuildResult = Depends(get_or_build_model)) -> ModelChildrenResponse:
    if model_id not in model.nodes_by_id:
        raise i3x_http_error(404, "ModelNotFound", f"Model id '{model_id}' does not exist")
    child_ids = model.children_by_id.get(model_id, [])
    children = [model.nodes_by_id[child_id] for child_id in child_ids if child_id in model.nodes_by_id]
    return ModelChildrenResponse(parent_id=model_id, children=children)
