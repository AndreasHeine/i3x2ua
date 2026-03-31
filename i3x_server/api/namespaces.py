from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from i3x_server.dependencies import get_opcua_client
from i3x_server.errors import i3x_http_error
from i3x_server.opcua.client import OpcUaClientProtocol

router = APIRouter(prefix="/namespaces", tags=["namespaces"])


class NamespacesResponse(BaseModel):
    count: int
    items: list[str]


@router.get("", response_model=NamespacesResponse)
async def get_namespaces(
    request: Request,
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> NamespacesResponse:
    _ = request
    try:
        namespaces = await opcua_client.get_namespaces()
    except Exception as exc:
        raise i3x_http_error(
            502,
            "OpcUaNamespaceError",
            "Failed to read OPC UA namespaces",
            {"cause": str(exc)},
        ) from exc
    return NamespacesResponse(count=len(namespaces), items=namespaces)
