"""MCP resources adapter: ``resources/list``/``resources/read`` handlers, helper routes, and guides."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Never

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from i3x_server.api.mcp.jsonrpc import MCP_ROOT_RESOURCE_PREFIX, _jsonrpc_error, _jsonrpc_response
from i3x_server.api.mcp.prompts import _prompt_registry
from i3x_server.api.mcp.roots import _roots_catalog
from i3x_server.api.v1.contracts import _status_title
from i3x_server.errors import i3x_http_error
from i3x_server.prompts.api import get_prompt, list_prompt_metadata

router = APIRouter()
logger = logging.getLogger(__name__)

MCP_MAPPING_GUIDE_URI = "i3x://opcua-to-i3x-mapping"
MCP_IDENTITY_GUIDE_URI = "i3x://element-id-and-type-id"
MCP_MODEL_OVERVIEW_URI = "i3x://model/overview"
MCP_MAPPING_GUIDE = """# OPC UA to i3X mapping guide

i3x2ua converts the connected OPC UA address space into an i3X graph. Treat
`elementId` and relationship IDs as opaque identifiers; use the IDs returned by
the server rather than deriving or guessing them.

## Nodes and types

- OPC UA instance nodes are exposed as i3X objects. Variables represent values
  and properties in the model; ObjectTypes describe the schema of those objects.
- `elementId` is generated deterministically from the expanded OPC UA NodeId,
  using the namespace URI rather than a session-specific namespace index.
- `typeElementId` links an object to its i3X ObjectType. OPC UA type-definition
  and DataType identities are preserved in metadata such as `sourceTypeId`,
  `uaTypeNodeId`, and `namespaceUri`.
- A missing type is synthesized from available DataType metadata when possible;
  otherwise an `UnknownType` placeholder preserves the unresolved reference.

## References become three relationship planes

- **Hierarchy** represents structural parent/child organization, such as
  `Plant -> Line -> Pump`. `Organizes` and hierarchical references generally
  map here. Parent selection is deterministic.
- **Composition** represents parts or properties belonging to an object, such
  as `Pump -> Temperature`. `HasProperty` maps here. `HasComponent` and
  `HasOrderedComponent` map Variable targets to composition; non-Variable
  targets map to hierarchy.
- **Graph** preserves non-hierarchical links between objects. OPC UA
  `NonHierarchicalReferences` and their subtypes map here; graph links do not
  determine object parents.

Reference classification follows the OPC UA reference type's supertype chain.
If a reference type cannot be classified, it is retained as a graph
relationship. `HasTypeDefinition` and `HasSubtype` describe type metadata, not
instance-to-instance edges. Built-in i3X relationships express hierarchy and
composition; discovered graph relationship names come from OPC UA
ReferenceType BrowseNames.

## Reading the graph

Start with `getObjectsV1` to discover real object IDs, then use
`queryRelatedObjectsV1` to navigate hierarchy, composition, and graph links.
Value and history reads recurse through composition only: hierarchy navigation
does not implicitly read values from descendants. Use `maxDepth=1` for the
selected object, `maxDepth=2` for direct composition children, and `maxDepth=0`
for unlimited composition traversal.
"""
MCP_IDENTITY_GUIDE = """# i3X ElementId and type identifiers

## Keep the identifiers distinct

- **`elementId`** identifies one i3X element (an object instance or an
  ObjectType). For an object instance, use it to fetch or navigate that exact
  node. It is an i3X identifier, not the OPC UA NodeId.
- **`typeElementId`** on an object identifies the i3X type associated with that
  instance. Use it to find the corresponding ObjectType in the i3X type
  catalog. It is a reference to an i3X type, not a second ID for the instance.
- **`sourceTypeId`** is OPC UA provenance metadata, not an i3X type identifier.
  For Objects it normally records the OPC UA TypeDefinition NodeId; for
  Variables, the mapped `typeElementId` is based on the OPC UA DataType.
  `uaTypeNodeId` also records the source TypeDefinition when present.
- **OPC UA `NodeId` / `ExpandedNodeId`** identifies the source node in the OPC
  UA address space. The raw NodeId is retained as `metadata.uaNodeId`; its
  expanded form is retained as `metadata.expandedNodeId`.

ObjectTypes also have their own i3X `elementId`. An object's `typeElementId`
refers to that ID. A type's OPC UA identity is exposed separately as its
`sourceTypeId` and OPC UA schema metadata; do not substitute that provenance
value for the i3X `typeElementId`.

## How object instance elementIds are generated

The mapper first converts the source NodeId to an ExpandedNodeId using the
server's OPC UA namespace table:

1. Replace `ns=N` with the namespace URI, yielding `nsu=<namespace-uri>`.
   Namespace index zero uses `http://opcfoundation.org/UA/`. For example,
   `ns=2;s=Pump-17` becomes
   `nsu=urn:example:plant;s=Pump-17` when namespace index 2 resolves to
   `urn:example:plant`.
2. Keep the identifier type and value (`i=` numeric, `s=` string, `g=` GUID,
   or `b=` ByteString) as part of the expanded string.
3. Compute SHA-1 over the UTF-8 bytes of that expanded string and take the
   first 16 lowercase hexadecimal characters.
4. Prefix the digest with the i3X kind and a hyphen:
   `<kind>-<digest>`. OPC UA Objects map to `asset`, Variables to `property`,
   Methods to `action`, and event-notifier nodes to `eventSource`.

Thus the example ID is `asset-<first-16-hex-digits-of-SHA1-of-the-expanded-string>`.
The hash input is the expanded NodeId string, not the BrowseName, DisplayName,
TypeDefinition, or raw namespace-index NodeId. Resolving the namespace URI
before hashing keeps the ID stable when OPC UA namespace indexes change
between connections. If a namespace index cannot be resolved, the mapper
retains the original NodeId string, so that stability guarantee cannot be
made for that unresolved namespace.

Use IDs returned by the server rather than calculating or guessing them.
Type IDs may be synthesized from OPC UA type metadata and are not necessarily
generated by the same instance-node hashing rule.
"""


class McpResourceReadRequest(BaseModel):
    uri: str


def _raise_invalid_resource(uri: str) -> Never:
    raise i3x_http_error(404, "Not Found", f"Unknown resource {uri}")


def _helper_error_payload(status: int, message: str) -> dict[str, Any]:
    """Error envelope matching the gateway exception handler, built without exception text."""
    return {
        "success": False,
        "error": {"code": status, "message": message},
        "responseDetail": {"title": _status_title(status), "status": status, "detail": message},
    }


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _resource_catalog(request: Request) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = [
        {
            "uri": MCP_MODEL_OVERVIEW_URI,
            "name": "i3X model overview",
            "description": "Current model summary, element counts, relationship counts, and navigation guidance.",
            "mimeType": "application/json",
        },
        {
            "uri": MCP_MAPPING_GUIDE_URI,
            "name": "OPC UA to i3X mapping guide",
            "description": (
                "Short guide to how OPC UA nodes and references map into i3X objects, types, "
                "Hierarchy, Composition, and Graph relationships."
            ),
            "mimeType": "text/markdown",
        },
        {
            "uri": MCP_IDENTITY_GUIDE_URI,
            "name": "i3X ElementId and type IDs",
            "description": (
                "Explains elementId, typeElementId, and sourceTypeId, including how OPC UA NodeIds "
                "become stable i3X instance IDs."
            ),
            "mimeType": "text/markdown",
        },
        {
            "uri": "i3x://openapi",
            "name": "OpenAPI specification",
            "description": "Server OpenAPI JSON document",
            "mimeType": "application/json",
        },
        {
            "uri": "i3x://mcp-overrides",
            "name": "MCP overrides",
            "description": (
                "Contents of overrides/mcp_overrides.json wrapped as {applied, contents}. "
                "An invalid file is ignored by the runtime, so `applied=false` means the "
                "effective catalog differs from these contents"
            ),
            "mimeType": "application/json",
        },
    ]

    model_cache = getattr(request.app.state, "model_cache", None)
    root_nodes = getattr(model_cache, "nodes_by_id", {})
    for root in _roots_catalog(request):
        root_id = root["uri"].removeprefix(MCP_ROOT_RESOURCE_PREFIX)
        if not isinstance(root_nodes, Mapping) or root_id not in root_nodes:
            continue
        resources.append(
            {
                "uri": root["uri"],
                "name": root["name"],
                "description": "Top-level i3X model element. Read this resource for a summary and navigation hints.",
                "mimeType": "application/json",
            }
        )

    for prompt in list_prompt_metadata(_prompt_registry(request)):
        resources.append(
            {
                "uri": f"i3x://prompts/{prompt['name']}",
                "name": f"Prompt: {prompt['name']}",
                "description": prompt["description"],
                "mimeType": "application/json",
            }
        )

    return resources


def _model_overview_content(request: Request) -> dict[str, Any]:
    model = getattr(request.app.state, "model_cache", None)
    nodes_by_id = getattr(model, "nodes_by_id", None)
    root_ids = getattr(model, "root_ids", None)
    if not isinstance(nodes_by_id, Mapping) or not isinstance(root_ids, list):
        overview: dict[str, Any] = {
            "modelAvailable": False,
            "message": "The i3X model has not been built or loaded yet.",
            "navigation": (
                "Use getObjectsV1(root=true) to discover model roots, then queryRelatedObjectsV1 or "
                "getChildren to navigate the model."
            ),
        }
    else:
        nodes = [node for node in nodes_by_id.values() if node is not None]
        kind_counts: dict[str, int] = {}
        for node in nodes:
            kind = getattr(node, "kind", None)
            if isinstance(kind, str):
                kind_counts[kind] = kind_counts.get(kind, 0) + 1

        edge_counts: dict[str, int] = {}
        for plane, attribute in (
            ("hierarchy", "hierarchy_children_by_id"),
            ("composition", "composition_children_by_id"),
            ("graph", "graph_related_by_id"),
        ):
            edges_by_id = getattr(model, attribute, {})
            edge_counts[plane] = (
                sum(len(edges) for edges in edges_by_id.values()) if isinstance(edges_by_id, Mapping) else 0
            )

        root_summaries: list[dict[str, Any]] = []
        for root_id in root_ids:
            node = nodes_by_id.get(root_id) if isinstance(root_id, str) else None
            if node is None:
                continue
            root_summaries.append(
                {
                    "uri": f"{MCP_ROOT_RESOURCE_PREFIX}{root_id}",
                    "elementId": root_id,
                    "displayName": getattr(node, "name", root_id),
                    "kind": getattr(node, "kind", "unknown"),
                    "typeElementId": (
                        getattr(node, "type", None)
                        or getattr(node, "source_type_id", None)
                        or getattr(node, "kind", "unknown")
                    ),
                    "namespaceUri": getattr(model, "namespace_uri_by_id", {}).get(root_id)
                    if isinstance(getattr(model, "namespace_uri_by_id", {}), Mapping)
                    else None,
                }
            )

        overview = {
            "modelAvailable": True,
            "revision": getattr(model, "revision", ""),
            "buildCompletedAtUtc": getattr(model, "build_completed_at_utc", None),
            "summary": {
                "totalElements": len(nodes),
                "rootCount": len(root_summaries),
                "elementsByKind": dict(sorted(kind_counts.items())),
                "relationshipEdges": edge_counts,
            },
            "roots": root_summaries,
            "navigation": (
                "Start from a listed root or read its i3x://roots/{elementId} resource for a summary. "
                "Use getChildren or queryRelatedObjectsV1 to navigate relevant branches. "
                "Use searchModelElements or findElementsByName when the target is unknown. "
                "Read values only after discovering the exact property elementId; value/history maxDepth "
                "traverses composition, not hierarchy."
            ),
        }

    return {
        "uri": MCP_MODEL_OVERVIEW_URI,
        "mimeType": "application/json",
        "text": json.dumps(overview, ensure_ascii=False),
    }


def _root_resource_content(request: Request, uri: str) -> dict[str, Any]:
    root_id = uri.removeprefix(MCP_ROOT_RESOURCE_PREFIX)
    model = getattr(request.app.state, "model_cache", None)
    nodes_by_id = getattr(model, "nodes_by_id", None)
    root_ids = getattr(model, "root_ids", None)
    if not root_id or not isinstance(root_ids, list) or root_id not in root_ids or not isinstance(nodes_by_id, Mapping):
        _raise_invalid_resource(uri)

    node = nodes_by_id.get(root_id)
    if node is None:
        _raise_invalid_resource(uri)

    namespace_uri_by_id = getattr(model, "namespace_uri_by_id", {})
    summary = {
        "elementId": root_id,
        "displayName": getattr(node, "name", root_id),
        "kind": getattr(node, "kind", "unknown"),
        "semanticRole": getattr(model, "semantic_role_by_id", {}).get(
            root_id,
            getattr(node, "semantic_role", "unknown"),
        )
        if isinstance(getattr(model, "semantic_role_by_id", {}), Mapping)
        else getattr(node, "semantic_role", "unknown"),
        "typeElementId": getattr(node, "type", None)
        or getattr(node, "source_type_id", None)
        or getattr(node, "kind", "unknown"),
        "namespaceUri": namespace_uri_by_id.get(root_id) if isinstance(namespace_uri_by_id, Mapping) else None,
        "relationshipCounts": {
            plane: len(getattr(model, attribute, {}).get(root_id, []))
            if isinstance(getattr(model, attribute, {}), Mapping)
            else 0
            for plane, attribute in (
                ("hierarchyChildren", "hierarchy_children_by_id"),
                ("compositionChildren", "composition_children_by_id"),
                ("graphRelations", "graph_related_by_id"),
            )
        },
        "navigation": (
            "Use getChildren or queryRelatedObjectsV1 with this elementId to explore connected elements. "
            "For values, discover exact property elementIds first; hierarchy traversal does not imply "
            "value/history traversal."
        ),
    }
    return {
        "uri": uri,
        "mimeType": "application/json",
        "text": json.dumps(summary, ensure_ascii=False),
    }


def _read_resource_content(request: Request, uri: str) -> dict[str, Any]:
    project_root = _project_root()
    if uri == MCP_MODEL_OVERVIEW_URI:
        return _model_overview_content(request)

    if uri.startswith(MCP_ROOT_RESOURCE_PREFIX):
        return _root_resource_content(request, uri)

    if uri == MCP_MAPPING_GUIDE_URI:
        return {
            "uri": uri,
            "mimeType": "text/markdown",
            "text": MCP_MAPPING_GUIDE,
        }

    if uri == MCP_IDENTITY_GUIDE_URI:
        return {
            "uri": uri,
            "mimeType": "text/markdown",
            "text": MCP_IDENTITY_GUIDE,
        }

    if uri == "i3x://openapi":
        return {
            "uri": uri,
            "mimeType": "application/json",
            "text": json.dumps(request.app.openapi(), ensure_ascii=False),
        }

    if uri == "i3x://mcp-overrides":
        path = project_root / "overrides" / "mcp_overrides.json"
        if not path.exists():
            _raise_invalid_resource(uri)
        raw = path.read_text(encoding="utf-8")
        applied = bool(getattr(request.app.state, "mcp_overrides_applied", False))
        try:
            document: dict[str, Any] = {"applied": applied, "contents": json.loads(raw)}
        except json.JSONDecodeError as exc:
            # A file the runtime could not parse is never applied; expose the raw text too.
            document = {"applied": False, "parseError": str(exc), "contents": raw}
        return {
            "uri": uri,
            "mimeType": "application/json",
            "text": json.dumps(document, ensure_ascii=False),
        }

    prompt_prefix = "i3x://prompts/"
    if uri.startswith(prompt_prefix):
        prompt_name = uri.removeprefix(prompt_prefix)
        prompt = get_prompt(_prompt_registry(request), prompt_name)
        return {
            "uri": uri,
            "mimeType": "application/json",
            "text": json.dumps(prompt, ensure_ascii=False),
        }

    _raise_invalid_resource(uri)


async def handle_resources_list(request: Request, message_id: Any, params: Any) -> dict[str, Any]:
    return _jsonrpc_response(message_id, {"resources": _resource_catalog(request)})


async def handle_resources_read(request: Request, message_id: Any, params: Any) -> dict[str, Any]:
    if not isinstance(params, dict):
        return _jsonrpc_error(message_id, -32602, "Invalid params")
    resource_uri = params.get("uri")
    if not isinstance(resource_uri, str) or not resource_uri:
        return _jsonrpc_error(message_id, -32602, "Invalid params")
    try:
        content = _read_resource_content(request, resource_uri)
    except HTTPException as exc:
        return _jsonrpc_error(message_id, -32002, "Resource not found", exc.detail)
    return _jsonrpc_response(message_id, {"contents": [content]})


@router.get("/resources", tags=["MCP Helpers"])
async def list_resources(request: Request) -> dict[str, Any]:
    return {"resources": _resource_catalog(request)}


@router.post("/resources/read", tags=["MCP Helpers"], response_model=dict[str, Any])
async def read_resource(request: Request, payload: McpResourceReadRequest) -> Response:
    # Exception text must not flow into the client response (CodeQL py/stack-trace-exposure):
    # failures are answered with a sanitized envelope built from the caller's own uri, and
    # unexpected errors are logged server-side only, mirroring the gateway's global handler.
    try:
        content = _read_resource_content(request, payload.uri)
    except HTTPException as exc:
        status = int(exc.status_code)
        message = f"Unknown resource {payload.uri}" if status == 404 else "Request failed"
        return JSONResponse(status_code=status, content=_helper_error_payload(status, message))
    except Exception:
        logger.exception("MCP resource read failed uri=%s", payload.uri)
        return JSONResponse(status_code=500, content=_helper_error_payload(500, "Internal server error"))
    return JSONResponse(content={"contents": [content]})
