from fastapi import APIRouter, Depends, Request

from i3x_server.dependencies import get_opcua_client, get_or_build_model
from i3x_server.errors import i3x_http_error
from i3x_server.schemas.i3x import ActionInvokeRequest, ActionInvokeResponse
from i3x_server.schemas.state import BuildResult

router = APIRouter(prefix="/action", tags=["action"])


@router.post("/{action_id}/invoke", response_model=ActionInvokeResponse)
async def invoke_action(
    action_id: str,
    payload: ActionInvokeRequest,
    request: Request,
    model: BuildResult = Depends(get_or_build_model),
) -> ActionInvokeResponse:
    target = model.action_to_method.get(action_id)
    if target is None:
        raise i3x_http_error(404, "ActionNotFound", f"Action id '{action_id}' does not exist")

    object_node_id, method_node_id = target
    opcua_client = get_opcua_client(request)
    try:
        result = await opcua_client.call_method(object_node_id, method_node_id, payload.args)
    except Exception as exc:
        raise i3x_http_error(502, "OpcUaActionError", "Failed to invoke OPC UA method", {"cause": str(exc)}) from exc
    return ActionInvokeResponse(action_id=action_id, result=result)
