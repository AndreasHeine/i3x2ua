import re

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from i3x_server.dependencies import get_opcua_client
from i3x_server.errors import i3x_http_error
from i3x_server.opcua.client import OpcUaClientProtocol

router = APIRouter(prefix="/objects", tags=["objects"])


class ObjectSchema(BaseModel):
    type: str = "object"
    description: str | None = None


class ObjectItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    elementId: str
    displayName: str
    namespaceUri: str
    schema_: ObjectSchema = Field(alias="schema")


@router.get("", response_model=list[ObjectItem])
async def get_objects(
    includeMetadata: bool = Query(default=False),
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> list[ObjectItem]:
    try:
        object_types = await opcua_client.get_object_types()
        namespace_infos = await opcua_client.get_namespace_infos()
    except Exception as exc:
        raise i3x_http_error(
            502,
            "OpcUaObjectsError",
            "Failed to read OPC UA objects",
            {"cause": str(exc)},
        ) from exc

    namespace_uris = [item.uri for item in namespace_infos]

    items: list[ObjectItem] = []
    for object_type in object_types:
        namespace_uri = _namespace_uri_for_node_id(object_type.node_id, namespace_uris)
        description = f"Derived from OPC UA ObjectType {object_type.display_name}" if includeMetadata else None
        items.append(
            ObjectItem.model_validate(
                {
                    "elementId": _to_element_id(object_type.browse_name),
                    "displayName": object_type.display_name,
                    "namespaceUri": namespace_uri,
                    "schema": ObjectSchema(type="object", description=description),
                }
            )
        )

    return items


def _namespace_uri_for_node_id(node_id: str, namespace_uris: list[str]) -> str:
    match = re.search(r"ns=(\d+)", node_id)
    namespace_index = int(match.group(1)) if match is not None else 0
    if 0 <= namespace_index < len(namespace_uris):
        return namespace_uris[namespace_index]
    return ""


def _to_element_id(name: str) -> str:
    normalized = re.sub(r"Type$", "-type", name)
    split = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", normalized)
    lowered = split.replace("_", "-").lower()
    compact = re.sub(r"-+", "-", lowered).strip("-")
    return compact or "unknown-type"
