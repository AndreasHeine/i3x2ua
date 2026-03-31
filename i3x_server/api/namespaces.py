from urllib.parse import urlparse

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from i3x_server.dependencies import get_opcua_client
from i3x_server.errors import i3x_http_error
from i3x_server.opcua.client import OpcUaClientProtocol, OpcUaNamespaceInfo

router = APIRouter(prefix="/namespaces", tags=["namespaces"])


class NamespaceItem(BaseModel):
    uri: str
    displayName: str


@router.get("", response_model=list[NamespaceItem])
async def get_namespaces(opcua_client: OpcUaClientProtocol = Depends(get_opcua_client)) -> list[NamespaceItem]:
    try:
        namespace_infos = await opcua_client.get_namespace_infos()
    except Exception as exc:
        raise i3x_http_error(
            502,
            "OpcUaNamespaceError",
            "Failed to read OPC UA namespaces",
            {"cause": str(exc)},
        ) from exc
    return [_to_namespace_item(item) for item in namespace_infos]


def _to_namespace_item(item: OpcUaNamespaceInfo) -> NamespaceItem:
    display_name = item.display_name or _display_name_for_uri(item.uri)
    return NamespaceItem(uri=item.uri, displayName=display_name)


def _display_name_for_uri(uri: str) -> str:
    lower = uri.lower()
    if "cesmii.org/i3x" in lower:
        return "I3X"
    if "isa.org/isa95" in lower:
        return "ISA95"
    if "abelara.com" in lower and lower.rstrip("/").endswith("/equipment"):
        return "Abelara Equipment"
    if "thinkiq.com" in lower and lower.rstrip("/").endswith("/equipment"):
        return "ThinkIQ Equipment"

    parsed = urlparse(uri)
    path_parts = [part for part in parsed.path.split("/") if part]
    if path_parts:
        token = path_parts[-1].replace("-", " ").replace("_", " ")
        if any(ch.isdigit() for ch in token):
            return token.upper()
        return token.title()

    host = parsed.netloc or uri
    base = host.split(":", 1)[0].split(".")
    if not base:
        return uri
    return base[0].title()
