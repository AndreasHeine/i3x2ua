from fastapi import APIRouter, Depends
from pydantic import BaseModel

from i3x_server.dependencies import get_opcua_client
from i3x_server.errors import i3x_http_error
from i3x_server.opcua.client import OpcUaClientProtocol

router = APIRouter(prefix="/objecttypes", tags=["objecttypes"])


class ObjectTypeItem(BaseModel):
    node_id: str
    parent_node_id: str | None
    browse_name: str
    display_name: str


@router.get("", response_model=list[ObjectTypeItem])
async def get_object_types(opcua_client: OpcUaClientProtocol = Depends(get_opcua_client)) -> list[ObjectTypeItem]:
    try:
        object_types = await opcua_client.get_object_types()
    except Exception as exc:
        raise i3x_http_error(
            502,
            "OpcUaObjectTypesError",
            "Failed to read OPC UA object types",
            {"cause": str(exc)},
        ) from exc

    return [
        ObjectTypeItem(
            node_id=item.node_id,
            parent_node_id=item.parent_node_id,
            browse_name=item.browse_name,
            display_name=item.display_name,
        )
        for item in object_types
    ]
