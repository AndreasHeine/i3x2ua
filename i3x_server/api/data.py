from fastapi import APIRouter, Depends, Request

from i3x_server.dependencies import get_opcua_client, get_or_build_model
from i3x_server.errors import i3x_http_error
from i3x_server.schemas.i3x import DataQueryRequest, DataQueryResponse, DataValueResponse
from i3x_server.schemas.state import BuildResult

router = APIRouter(prefix="/data", tags=["data"])


async def _read_property(model: BuildResult, property_id: str, request: Request) -> DataValueResponse:
    node_id = model.property_to_node.get(property_id)
    if node_id is None:
        raise i3x_http_error(404, "PropertyNotFound", f"Property id '{property_id}' does not exist")
    opcua_client = get_opcua_client(request)
    try:
        value = await opcua_client.read_value(node_id)
    except Exception as exc:
        raise i3x_http_error(502, "OpcUaReadError", "Failed to read OPC UA property", {"cause": str(exc)}) from exc
    return DataValueResponse(property_id=property_id, value=value)


@router.get("/{property_id}", response_model=DataValueResponse)
async def get_data_value(
    property_id: str,
    request: Request,
    model: BuildResult = Depends(get_or_build_model),
) -> DataValueResponse:
    return await _read_property(model, property_id, request)


@router.post("/query", response_model=DataQueryResponse)
async def query_data_values(
    payload: DataQueryRequest,
    request: Request,
    model: BuildResult = Depends(get_or_build_model),
) -> DataQueryResponse:
    values = [await _read_property(model, prop_id, request) for prop_id in payload.property_ids]
    return DataQueryResponse(values=values)
