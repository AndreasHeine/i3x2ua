from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field
from typing_extensions import Never

from i3x_server.errors import i3x_http_error
from i3x_server.mcp.invocation import call_mcp_tool, invoke_mcp_tool
from i3x_server.prompts.api import execute_prompt, get_prompt, list_prompt_metadata
from i3x_server.prompts.registry import PromptRegistry
from i3x_server.version import get_server_version

router = APIRouter(prefix="/mcp")
logger = logging.getLogger(__name__)
MCP_PROTOCOL_VERSION = "2026-07-28"
MCP_LEGACY_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
MCP_LEGACY_PROTOCOL_VERSION = MCP_LEGACY_PROTOCOL_VERSIONS[0]
MCP_SUPPORTED_MODERN_PROTOCOL_VERSIONS = (MCP_PROTOCOL_VERSION,)
MCP_SUPPORTED_PROTOCOL_VERSIONS = (MCP_PROTOCOL_VERSION, *MCP_LEGACY_PROTOCOL_VERSIONS)
MCP_META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
MCP_META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
MCP_CACHEABLE_RESULT_METHODS = {
    "tools/list",
    "prompts/list",
    "resources/list",
    "resources/templates/list",
    "resources/read",
}
MCP_MAPPING_GUIDE_URI = "i3x://opcua-to-i3x-mapping"
MCP_IDENTITY_GUIDE_URI = "i3x://element-id-and-type-id"
MCP_MODEL_OVERVIEW_URI = "i3x://model/overview"
MCP_ROOT_RESOURCE_PREFIX = "i3x://roots/"
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
MCP_INSTRUCTIONS = (
    "Discover elementIds before querying values. Value/history maxDepth traverses composition only, "
    "not hierarchy: 1 reads only the object, 2 includes direct components, 0 is unlimited. "
    "For many values below one known container, call readContainerValues with its exact containerId "
    "instead of generating a long elementIds array. "
    "Preserve value, quality, and source timestamp; latest known values need not be fresh. "
    "Inspect per-item failures even when HTTP succeeds. PUT/write operations are REST-only. "
    "Subscription tools change monitoring state; acknowledge only updates already processed. "
    "Plant roots are available using getObjectsV1(root=true), not MCP filesystem roots."
)
MCP_SESSION_HEADER = "Mcp-Session-Id"
MCP_SSE_KEEPALIVE_SECONDS = 15


@dataclass(slots=True)
class McpTransportSession:
    protocol_version: str = MCP_LEGACY_PROTOCOL_VERSION
    streams: set[asyncio.Queue[dict[str, Any] | None]] = field(default_factory=set)


def publish_mcp_notification(
    app: FastAPI,
    session_id: str,
    method: str,
    params: dict[str, Any] | None = None,
) -> int:
    if not method.startswith("notifications/") or len(method) == len("notifications/"):
        raise ValueError("MCP server notifications must use a notifications/ method")
    sessions = getattr(app.state, "mcp_transport_sessions", {})
    session = sessions.get(session_id) if isinstance(sessions, dict) else None
    if not isinstance(session, McpTransportSession):
        return 0
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    for stream in tuple(session.streams):
        stream.put_nowait(message)
    return len(session.streams)


def shutdown_mcp_transport(app: FastAPI) -> None:
    sessions = getattr(app.state, "mcp_transport_sessions", {})
    if not isinstance(sessions, dict):
        return
    for session in sessions.values():
        if isinstance(session, McpTransportSession):
            for stream in tuple(session.streams):
                stream.put_nowait(None)
    sessions.clear()


def _mcp_sessions(request: Request) -> dict[str, McpTransportSession]:
    sessions = getattr(request.app.state, "mcp_transport_sessions", None)
    if not isinstance(sessions, dict):
        sessions = {}
        request.app.state.mcp_transport_sessions = sessions
    return sessions


def _origin_is_allowed(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True
    allowed_origins = getattr(request.app.state, "mcp_allowed_origins", [])
    same_local_origin = origin == str(request.base_url).rstrip("/") and urlsplit(origin).hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }
    return same_local_origin or (origin != "*" and origin in allowed_origins)


class McpCallRequest(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class McpPromptExecuteRequest(BaseModel):
    name: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class McpResourceReadRequest(BaseModel):
    uri: str


class McpJsonRpcErrorDetail(BaseModel):
    code: int
    message: str
    data: Any = None


class McpJsonRpcSuccess(BaseModel):
    jsonrpc: Literal["2.0"]
    id: Any
    result: Any


class McpJsonRpcError(BaseModel):
    jsonrpc: Literal["2.0"]
    id: Any
    error: McpJsonRpcErrorDetail


def _jsonrpc_response(result_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": result_id, "result": result}


def _jsonrpc_error(result_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": result_id, "error": error}


def _decode_modern_header_value(value: str) -> str | None:
    prefix = "=?base64?"
    if not value.startswith(prefix) or not value.endswith("?="):
        return value
    try:
        return base64.b64decode(value[len(prefix) : -2], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None


def _modern_request_error(request: Request, message: Mapping[str, Any]) -> dict[str, Any] | None:
    params = message.get("params")
    metadata = params.get("_meta") if isinstance(params, Mapping) else None
    metadata = metadata if isinstance(metadata, Mapping) else {}
    protocol_version = metadata.get(MCP_META_PROTOCOL_VERSION)
    protocol_header = request.headers.get("mcp-protocol-version")
    method = message.get("method")
    method_header = request.headers.get("mcp-method")
    name_header = request.headers.get("mcp-name")
    request_id = message.get("id")

    def header_mismatch(detail: str) -> dict[str, Any]:
        return _jsonrpc_error(request_id, -32020, f"HeaderMismatch: {detail}")

    if not isinstance(protocol_version, str) or not isinstance(metadata.get(MCP_META_CLIENT_CAPABILITIES), dict):
        return _jsonrpc_error(request_id, -32602, "Missing or invalid per-request MCP metadata")
    if protocol_header is None or protocol_header != protocol_version:
        return header_mismatch("MCP-Protocol-Version does not match request metadata")
    if not isinstance(method, str) or method_header != method:
        return header_mismatch("Mcp-Method does not match request method")

    params = params if isinstance(params, Mapping) else {}
    expected_name: Any = None
    if method in {"tools/call", "prompts/get"}:
        expected_name = params.get("name")
    elif method == "resources/read":
        expected_name = params.get("uri")
    if isinstance(expected_name, str):
        decoded_name = _decode_modern_header_value(name_header) if name_header is not None else None
        if decoded_name != expected_name:
            return header_mismatch("Mcp-Name does not match request name")
    elif name_header is not None:
        return header_mismatch("Mcp-Name is not applicable to this method")

    if protocol_version not in MCP_SUPPORTED_MODERN_PROTOCOL_VERSIONS:
        return _jsonrpc_error(
            request_id,
            -32022,
            "Unsupported protocol version",
            {
                "supported": list(MCP_SUPPORTED_PROTOCOL_VERSIONS),
                "requested": protocol_version,
            },
        )
    return None


def _add_modern_response_metadata(response: dict[str, Any], method: str) -> dict[str, Any]:
    result = response.get("result")
    if not isinstance(result, dict):
        return response
    result.setdefault("resultType", "complete")
    if method in MCP_CACHEABLE_RESULT_METHODS:
        result.setdefault("ttlMs", 0)
        result.setdefault("cacheScope", "private")
    metadata = result.get("_meta")
    if not isinstance(metadata, dict):
        metadata = {}
        result["_meta"] = metadata
    metadata[MCP_META_PROTOCOL_VERSION] = MCP_PROTOCOL_VERSION
    return response


def _raise_invalid_tool(tool_name: str) -> Never:
    raise i3x_http_error(400, "Bad Request", f"Unknown tool {tool_name}")


def _raise_invalid_resource(uri: str) -> Never:
    raise i3x_http_error(404, "Not Found", f"Unknown resource {uri}")


def _tool_catalog(request: Request) -> list[dict[str, Any]]:
    tools = getattr(request.app.state, "mcp_tools", {})
    if not isinstance(tools, Mapping):
        return []
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.input_schema,
            "outputSchema": tool.output_schema,
            "annotations": tool.annotations,
        }
        for tool in tools.values()
    ]


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
            "description": "Runtime MCP tool, prompt, and feature overrides",
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


def _roots_catalog(request: Request) -> list[dict[str, str]]:
    model_cache = getattr(request.app.state, "model_cache", None)
    if model_cache is None:
        return []

    root_ids = getattr(model_cache, "root_ids", [])
    nodes_by_id = getattr(model_cache, "nodes_by_id", {})
    roots: list[dict[str, str]] = []
    for root_id in root_ids:
        node = nodes_by_id.get(root_id)
        display_name = getattr(node, "name", str(root_id)) if node is not None else str(root_id)
        roots.append(
            {
                "uri": f"i3x://roots/{root_id}",
                "name": display_name,
            }
        )
    return roots


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


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
        return {
            "uri": uri,
            "mimeType": "application/json",
            "text": path.read_text(encoding="utf-8"),
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


def _prompt_registry(request: Request) -> PromptRegistry | None:
    registry = getattr(request.app.state, "mcp_prompts", None)
    return registry if isinstance(registry, PromptRegistry) else None


def _compact_object(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    return {key: item[key] for key in ("elementId", "displayName") if key in item}


def _compact_bulk_results(body: dict[str, Any], project_result: Any) -> dict[str, Any]:
    results = body.get("results")
    if not isinstance(results, list):
        return body
    compacted = dict(body)
    compacted["results"] = [
        {
            **item,
            **({"result": project_result(item["result"])} if item.get("success") and "result" in item else {}),
        }
        if isinstance(item, dict)
        else item
        for item in results
    ]
    return compacted


def _compact_tool_body(tool_name: str, body: Any) -> Any:
    if not isinstance(body, dict):
        return body

    if tool_name == "getObjectsV1" and isinstance(body.get("result"), list):
        compacted = dict(body)
        compacted["result"] = [_compact_object(item) for item in body["result"]]
        return compacted

    if tool_name == "listObjectsByIdV1":
        return _compact_bulk_results(body, _compact_object)

    if tool_name == "queryRelatedObjectsV1":

        def compact_related(result: Any) -> Any:
            if not isinstance(result, list):
                return result
            return [
                {
                    **related,
                    **({"object": _compact_object(related["object"])} if "object" in related else {}),
                }
                if isinstance(related, dict)
                else related
                for related in result
            ]

        return _compact_bulk_results(body, compact_related)

    if tool_name == "getObjectTypesV1" and isinstance(body.get("result"), list):
        compacted = dict(body)
        compacted["result"] = [
            {key: item[key] for key in ("elementId", "displayName", "namespaceUri", "version") if key in item}
            if isinstance(item, dict)
            else item
            for item in body["result"]
        ]
        return compacted

    if tool_name == "queryObjectTypesV1":
        return _compact_bulk_results(
            body,
            lambda result: (
                {key: value for key, value in result.items() if key != "related"}
                if isinstance(result, dict)
                else result
            ),
        )

    if tool_name in {"getRelationshipTypesV1", "queryRelationshipTypesV1"}:
        if tool_name == "getRelationshipTypesV1" and isinstance(body.get("result"), list):
            compacted = dict(body)
            compacted["result"] = [_compact_object(item) for item in body["result"]]
            return compacted
        if tool_name == "queryRelationshipTypesV1":
            return _compact_bulk_results(body, _compact_object)

    if tool_name == "queryLastKnownValuesV1":

        def compact_current(result: Any) -> Any:
            if not isinstance(result, dict):
                return result
            return {
                key: value
                for key, value in result.items()
                if key != "isComposition" and not (key == "components" and value is None)
            }

        return _compact_bulk_results(body, compact_current)

    if tool_name == "queryHistoricalValuesV1":

        def compact_history(result: Any) -> Any:
            if not isinstance(result, dict):
                return result
            return {
                key: value
                for key, value in result.items()
                if key != "isComposition" and not (key == "components" and value is None)
            }

        return _compact_bulk_results(body, compact_history)

    return body


def _tool_result(body: Any, status_code: int, tool_name: str | None = None) -> dict[str, Any]:
    if tool_name is not None:
        body = _compact_tool_body(tool_name, body)
    failed = status_code >= 400
    if isinstance(body, dict):
        results = body.get("results")
        failed = (
            failed
            or body.get("success") is False
            or any(
                isinstance(item, dict) and item.get("success") is False
                for item in (results if isinstance(results, list) else [])
            )
        )
    return {
        "content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}],
        "structuredContent": {"statusCode": status_code, "body": body},
        "isError": failed,
    }


async def _handle_jsonrpc(request: Request, message: dict[str, Any]) -> dict[str, Any] | None:
    jsonrpc = message.get("jsonrpc")
    message_id = message.get("id")
    method = message.get("method")
    params = message.get("params")

    if "id" in message and type(message_id) not in (str, int):
        return _jsonrpc_error(None, -32600, "Invalid Request")
    if jsonrpc != "2.0":
        return _jsonrpc_error(message_id, -32600, "Invalid Request")

    if not isinstance(method, str):
        return _jsonrpc_error(message_id, -32600, "Invalid Request")

    if message_id is None:
        return None
    if params is not None and not isinstance(params, dict):
        return _jsonrpc_error(message_id, -32602, "Invalid params")

    if method == "initialize":
        if not isinstance(params, dict) or not isinstance(params.get("protocolVersion"), str):
            return _jsonrpc_error(message_id, -32602, "protocolVersion must be a string")
        requested_version = params["protocolVersion"]
        negotiated_version = (
            requested_version if requested_version in MCP_LEGACY_PROTOCOL_VERSIONS else MCP_LEGACY_PROTOCOL_VERSION
        )
        return _jsonrpc_response(
            message_id,
            {
                "protocolVersion": negotiated_version,
                "serverInfo": {"name": "i3x", "version": get_server_version()},
                "capabilities": {
                    "tools": {"listChanged": False},
                    "prompts": {"listChanged": False},
                    "resources": {"listChanged": False},
                },
                "instructions": MCP_INSTRUCTIONS,
            },
        )

    if method == "server/discover":
        return _jsonrpc_response(
            message_id,
            {
                "supportedVersions": list(MCP_SUPPORTED_PROTOCOL_VERSIONS),
                "capabilities": {
                    "tools": {},
                    "prompts": {},
                    "resources": {},
                },
                "_meta": {
                    "io.modelcontextprotocol/serverInfo": {
                        "name": "i3x",
                        "version": get_server_version(),
                    }
                },
                "instructions": MCP_INSTRUCTIONS,
                "ttlMs": 3_600_000,
                "cacheScope": "public",
            },
        )

    if method == "resources/templates/list":
        return _jsonrpc_response(message_id, {"resourceTemplates": []})

    if method == "ping":
        return _jsonrpc_response(message_id, {})

    if method == "tools/list":
        return _jsonrpc_response(message_id, {"tools": _tool_catalog(request)})

    if method == "tools/call":
        if not isinstance(params, dict):
            return _jsonrpc_error(message_id, -32602, "Invalid params")
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(tool_name, str) or not isinstance(arguments, dict):
            return _jsonrpc_error(message_id, -32602, "Invalid params")
        tools = getattr(request.app.state, "mcp_tools", {})
        if not isinstance(tools, Mapping) or tool_name not in tools:
            return _jsonrpc_error(message_id, -32602, f"Unknown tool {tool_name}")
        tool = tools[tool_name]
        validation_error = next(Draft202012Validator(tool.input_schema).iter_errors(arguments), None)
        if validation_error is not None:
            return _jsonrpc_error(message_id, -32602, f"Invalid tool arguments: {validation_error.message}")
        try:
            payload = await invoke_mcp_tool(request, tool, arguments)
        except HTTPException as exc:
            if exc.status_code in {400, 422}:
                return _jsonrpc_error(message_id, -32602, "Invalid tool arguments", exc.detail)
            return _jsonrpc_response(message_id, _tool_result(exc.detail, exc.status_code))
        if not isinstance(payload, dict) or type(payload.get("status_code")) is not int or "body" not in payload:
            logger.error("Invalid MCP tool response tool=%s", tool_name)
            return _jsonrpc_error(message_id, -32603, "Internal error")
        status_code = payload["status_code"]
        body = payload["body"]
        return _jsonrpc_response(message_id, _tool_result(body, status_code, tool_name))

    if method == "prompts/list":
        prompts: list[dict[str, Any]] = [dict(item) for item in list_prompt_metadata(_prompt_registry(request))]
        registry = _prompt_registry(request)
        for metadata in prompts:
            prompt = registry.get(metadata["name"]) if registry is not None else None
            if prompt is not None:
                metadata["arguments"] = [{"name": name, "required": True} for name in prompt.inputs]
        return _jsonrpc_response(message_id, {"prompts": prompts})

    if method == "prompts/get":
        if not isinstance(params, dict):
            return _jsonrpc_error(message_id, -32602, "Invalid params")
        prompt_name = params.get("name")
        arguments = params.get("arguments", {})
        if (
            not isinstance(prompt_name, str)
            or not prompt_name
            or not isinstance(arguments, dict)
            or not all(isinstance(value, str) for value in arguments.values())
        ):
            return _jsonrpc_error(message_id, -32602, "Invalid params")
        try:
            rendered = execute_prompt(_prompt_registry(request), prompt_name, arguments)
        except HTTPException as exc:
            return _jsonrpc_error(message_id, -32602, "Invalid prompt request", exc.detail)
        return _jsonrpc_response(
            message_id,
            {
                "description": rendered["description"],
                "messages": [{"role": "user", "content": {"type": "text", "text": rendered["rendered"]}}],
            },
        )

    if method == "prompts/execute":
        if not isinstance(params, dict):
            return _jsonrpc_error(message_id, -32602, "Invalid params")
        prompt_name = params.get("name")
        prompt_parameters = params.get("parameters", {})
        if not isinstance(prompt_name, str) or not isinstance(prompt_parameters, dict):
            return _jsonrpc_error(message_id, -32602, "Invalid params")
        try:
            prompt_result = execute_prompt(_prompt_registry(request), prompt_name, prompt_parameters)
        except HTTPException as exc:
            return _jsonrpc_error(message_id, -32602, "Invalid prompt request", exc.detail)
        return _jsonrpc_response(message_id, prompt_result)

    if method == "resources/list":
        return _jsonrpc_response(message_id, {"resources": _resource_catalog(request)})

    if method == "resources/read":
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

    return _jsonrpc_error(message_id, -32601, f"Method not found: {method}")


@router.get("/tools", tags=["MCP Helpers"])
async def list_tools(request: Request) -> dict[str, Any]:
    tools = getattr(request.app.state, "mcp_tools", {})
    if isinstance(tools, Mapping):
        payload: dict[str, dict[str, Any]] = {}
        for name, tool in tools.items():
            item = tool.to_dict()
            # Keep legacy snake_case fields while exposing MCP-compatible camelCase.
            item["inputSchema"] = item.get("input_schema", {})
            item["pathParameters"] = item.get("path_parameters", [])
            item["queryParameters"] = item.get("query_parameters", [])
            item["bodyRequired"] = item.get("body_required", False)
            payload[name] = item
        return {"tools": payload}
    return {"tools": {}}


@router.post("/call", tags=["MCP Helpers"])
async def call_tool(request: Request, payload: McpCallRequest) -> Any:
    tools = getattr(request.app.state, "mcp_tools", {})
    if not isinstance(tools, Mapping) or payload.tool not in tools:
        _raise_invalid_tool(payload.tool)

    tool = tools[payload.tool]
    return await call_mcp_tool(request, tool, payload.arguments)


@router.get("/prompts", tags=["MCP Helpers"])
async def list_prompts(request: Request) -> dict[str, Any]:
    return {"prompts": list_prompt_metadata(_prompt_registry(request))}


@router.get("/prompts/{name}", tags=["MCP Helpers"])
async def get_prompt_definition(request: Request, name: str) -> dict[str, Any]:
    return get_prompt(_prompt_registry(request), name)


@router.post("/prompts/execute", tags=["MCP Helpers"])
async def execute_prompt_template(request: Request, payload: McpPromptExecuteRequest) -> dict[str, Any]:
    return execute_prompt(_prompt_registry(request), payload.name, payload.parameters)


@router.get("/resources", tags=["MCP Helpers"])
async def list_resources(request: Request) -> dict[str, Any]:
    return {"resources": _resource_catalog(request)}


@router.post("/resources/read", tags=["MCP Helpers"])
async def read_resource(request: Request, payload: McpResourceReadRequest) -> dict[str, Any]:
    return {"contents": [_read_resource_content(request, payload.uri)]}


@router.get("/roots", tags=["MCP Helpers"])
async def list_roots(request: Request) -> dict[str, Any]:
    return {"roots": _roots_catalog(request)}


@router.get(
    "",
    tags=["MCP Protocol"],
    responses={
        200: {
            "description": "Session-scoped server-sent event stream.",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        400: {"description": "Missing MCP session identifier."},
        404: {"description": "Unknown MCP session."},
        406: {"description": "The Accept header must include text/event-stream."},
    },
)
async def mcp_get(request: Request) -> Response:
    if not _origin_is_allowed(request):
        return JSONResponse(status_code=403, content=_jsonrpc_error(None, -32600, "Origin not allowed"))
    accepted_types = {
        item.split(";", maxsplit=1)[0].strip().lower() for item in request.headers.get("accept", "").split(",")
    }
    if "text/event-stream" not in accepted_types:
        return JSONResponse(status_code=406, content=_jsonrpc_error(None, -32600, "Accept: text/event-stream required"))

    session_id = request.headers.get(MCP_SESSION_HEADER)
    if not session_id:
        return JSONResponse(status_code=400, content=_jsonrpc_error(None, -32600, "Mcp-Session-Id required"))
    session = _mcp_sessions(request).get(session_id)
    if session is None:
        return JSONResponse(status_code=404, content=_jsonrpc_error(None, -32600, "Unknown MCP session"))

    stream: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    session.streams.add(stream)

    async def event_stream() -> AsyncIterator[str]:
        try:
            while True:
                try:
                    message = await asyncio.wait_for(stream.get(), timeout=MCP_SSE_KEEPALIVE_SECONDS)
                except TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": keepalive\n\n"
                    continue
                if message is None:
                    break
                yield f"event: message\ndata: {json.dumps(message, ensure_ascii=False)}\n\n"
        finally:
            session.streams.discard(stream)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            MCP_SESSION_HEADER: session_id,
        },
    )


@router.delete("", tags=["MCP Protocol"], status_code=200)
async def mcp_delete(request: Request) -> Response:
    if not _origin_is_allowed(request):
        return JSONResponse(status_code=403, content=_jsonrpc_error(None, -32600, "Origin not allowed"))
    session_id = request.headers.get(MCP_SESSION_HEADER)
    if not session_id:
        return JSONResponse(status_code=400, content=_jsonrpc_error(None, -32600, "Mcp-Session-Id required"))
    session = _mcp_sessions(request).pop(session_id, None)
    if session is None:
        return JSONResponse(status_code=404, content=_jsonrpc_error(None, -32600, "Unknown MCP session"))
    for stream in tuple(session.streams):
        stream.put_nowait(None)
    return Response(status_code=200)


@router.post(
    "",
    tags=["MCP Protocol"],
    response_class=JSONResponse,
    responses={
        200: {
            "model": McpJsonRpcSuccess | McpJsonRpcError,
            "description": "JSON-RPC 2.0 response object. Protocol errors are JSON-RPC error "
            "objects, not i3X error envelopes.",
            "content": {
                "application/json": {
                    "example": {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {"tools": []},
                    },
                }
            },
        },
        202: {
            "description": "Notification accepted; the HTTP response has no body.",
            "content": {},
        },
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"type": "object"},
                    "examples": {
                        "request": {
                            "summary": "List tools",
                            "value": {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                        },
                        "notification": {
                            "summary": "Initialized notification (HTTP 202)",
                            "value": {
                                "jsonrpc": "2.0",
                                "method": "notifications/initialized",
                            },
                        },
                    },
                }
            },
        },
    },
)
async def mcp_post(request: Request) -> Response:
    if not _origin_is_allowed(request):
        return JSONResponse(status_code=403, content=_jsonrpc_error(None, -32600, "Origin not allowed"))
    try:
        message = await request.json()
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(content=_jsonrpc_error(None, -32700, "Parse error"))

    if not isinstance(message, dict):
        return JSONResponse(content=_jsonrpc_error(None, -32600, "Invalid Request"))

    params = message.get("params")
    metadata = params.get("_meta") if isinstance(params, dict) else None
    metadata = metadata if isinstance(metadata, dict) else {}
    protocol_version = request.headers.get("mcp-protocol-version")
    modern_request = (
        protocol_version == MCP_PROTOCOL_VERSION
        or MCP_META_PROTOCOL_VERSION in metadata
        or message.get("method") == "server/discover"
        or (protocol_version is not None and protocol_version not in MCP_LEGACY_PROTOCOL_VERSIONS)
    )
    if modern_request:
        validation_error = _modern_request_error(request, message)
        if validation_error is not None:
            return JSONResponse(status_code=400, content=validation_error)
        if message.get("method") == "initialize":
            return JSONResponse(
                status_code=400,
                content=_jsonrpc_error(message.get("id"), -32601, "Method not found: initialize"),
            )
        modern = True
        session_id = None
        sessions = _mcp_sessions(request)
    else:
        modern = False
        if protocol_version is not None and protocol_version not in MCP_LEGACY_PROTOCOL_VERSIONS:
            return JSONResponse(
                status_code=400,
                content=_jsonrpc_error(None, -32600, "Unsupported protocol version"),
            )
        session_id = request.headers.get(MCP_SESSION_HEADER)
        sessions = _mcp_sessions(request)
        if session_id is not None and session_id not in sessions:
            return JSONResponse(status_code=404, content=_jsonrpc_error(None, -32600, "Unknown MCP session"))
        if session_id is not None and protocol_version is not None:
            if protocol_version != sessions[session_id].protocol_version:
                return JSONResponse(
                    status_code=400,
                    content=_jsonrpc_error(None, -32600, "MCP protocol version does not match session"),
                )

    try:
        response = await _handle_jsonrpc(request, message)
    except Exception:
        logger.exception("MCP request failed method=%s", message.get("method"))
        response = _jsonrpc_error(message.get("id"), -32603, "Internal error") if "id" in message else None
    if modern and response is not None and "result" in response:
        response = _add_modern_response_metadata(response, message["method"])
    response_headers: dict[str, str] = {}
    if not modern and message.get("method") == "initialize" and response is not None and "result" in response:
        session_id = uuid4().hex
        negotiated_version = response["result"].get("protocolVersion", MCP_LEGACY_PROTOCOL_VERSION)
        sessions[session_id] = McpTransportSession(protocol_version=negotiated_version)
        response_headers[MCP_SESSION_HEADER] = session_id
    elif session_id is not None and message.get("method") == "notifications/initialized":
        response_headers[MCP_SESSION_HEADER] = session_id
    if response is None:
        return Response(status_code=202, headers=response_headers)
    return JSONResponse(content=response, headers=response_headers)
