from __future__ import annotations

import json
import logging
import re
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from contextlib import nullcontext as _nullcontext
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from posixpath import normpath
from time import perf_counter
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from i3x_server.domain.utils import canonical_node_id
from i3x_server.errors import i3x_http_error
from i3x_server.model.revision import model_revision
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult


def _load_otel_tracing() -> tuple[Any, Any, Any, Any]:
    try:
        from opentelemetry import trace as otel_trace
        from opentelemetry.trace import Status as otel_status
        from opentelemetry.trace import StatusCode as otel_status_code
        from opentelemetry.trace import get_current_span
    except ImportError:  # pragma: no cover - optional dependency
        return None, None, None, None

    # Tracer is a proxy — it forwards to the global TracerProvider configured later.
    return (
        otel_status,
        otel_status_code,
        get_current_span,
        otel_trace.get_tracer("i3x_server.mcp"),
    )


_OtelStatus, _OtelStatusCode, get_current_span, _mcp_tracer = _load_otel_tracing()

# Meter instruments bind to the provider at creation time, so they are populated
# by init_mcp_metrics() after the provider is configured.
_mcp_tool_calls: Any = None
_mcp_tool_duration: Any = None

MCP_EXCLUDED_OPERATION_IDS = {"streamSubscription"}
MCP_PAGINATED_LIST_TOOLS = {"getObjectsV1", "getObjectTypesV1", "getRelationshipTypesV1"}
MCP_DEFAULT_PAGE_SIZE = 50
MCP_BODY_PAGINATED_TOOLS = {
    "queryRelatedObjectsV1": 50,
    "queryHistoricalValuesV1": 100,
}


MCP_CONTAINER_VALUES_TOOL = "readContainerValues"
MCP_CONTAINER_VALUES_BATCH_SIZE = 10
MCP_CONTAINER_VALUES_MAX_NODES = 5000
MCP_CONTAINER_VALUES_MAX_DEPTH = 32
MCP_MODEL_CONTEXT_MAX_ITEMS = 500
MCP_MODEL_PATH_MAX_NODES = 5000
MCP_ID_RESOLUTION_MAX_ITEMS = 500
MCP_MODEL_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "resolveElementIdsToNodeIds": {
        "type": "object",
        "properties": {
            "elementIds": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": MCP_ID_RESOLUTION_MAX_ITEMS,
            }
        },
        "required": ["elementIds"],
        "additionalProperties": False,
    },
    "resolveNodeIdsToElementIds": {
        "type": "object",
        "properties": {
            "nodeIds": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": MCP_ID_RESOLUTION_MAX_ITEMS,
            }
        },
        "required": ["nodeIds"],
        "additionalProperties": False,
    },
    "searchModelElements": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "Case-insensitive substring across cached names and metadata. "
                    "When omitted, returns the first page of the model."
                ),
            },
            "kind": {"type": "string", "enum": ["asset", "property", "action", "eventSource"]},
            "semanticRole": {
                "type": "string",
                "enum": ["asset", "group", "component", "datapoint", "property", "unknown"],
            },
            "namespaceUri": {"type": "string"},
            "typeElementId": {"type": "string"},
            "mappingConfidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "appliedProfileId": {"type": "string"},
            "relationshipType": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        },
        "additionalProperties": False,
    },
    "getElementContext": {
        "type": "object",
        "properties": {
            "elementId": {"type": "string", "minLength": 1},
            "ancestorDepth": {"type": "integer", "minimum": 0, "maximum": 32, "default": 3},
            "childLimit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            "relationshipLimit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            "includeChildren": {"type": "boolean", "default": True},
            "includeRelationships": {"type": "boolean", "default": True},
            "includeTypeAndMetadata": {"type": "boolean", "default": True},
        },
        "required": ["elementId"],
        "additionalProperties": False,
    },
    "summarizeSubtree": {
        "type": "object",
        "properties": {
            "rootElementId": {"type": "string", "minLength": 1},
            "maxDepth": {"type": "integer", "minimum": 0, "maximum": 32, "default": 4},
            "includeHierarchy": {"type": "boolean", "default": True},
            "includeComposition": {"type": "boolean", "default": True},
            "includeProperties": {"type": "boolean", "default": True},
            "includeActions": {"type": "boolean", "default": True},
            "maxNodes": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200},
        },
        "required": ["rootElementId"],
        "additionalProperties": False,
    },
    "traceRelationshipPath": {
        "type": "object",
        "properties": {
            "sourceElementId": {"type": "string", "minLength": 1},
            "targetElementId": {"type": "string", "minLength": 1},
            "relationshipPlanes": {
                "type": "array",
                "items": {"type": "string", "enum": ["hierarchy", "composition", "graph"]},
                "default": ["hierarchy", "composition", "graph"],
            },
            "direction": {"type": "string", "enum": ["outgoing", "incoming", "either"], "default": "either"},
            "maxDepth": {"type": "integer", "minimum": 0, "maximum": 32, "default": 8},
        },
        "required": ["sourceElementId", "targetElementId"],
        "additionalProperties": False,
    },
}
McpModelProvider = Callable[[Request], Awaitable[BuildResult]]
_MCP_INTERNAL_BASE_URL = httpx.URL("http://mcp.local")
_TRACEPARENT_PATTERN = re.compile(
    r"^[\da-f]{2}-([\da-f]{32})-([\da-f]{16})-[\da-f]{2}$",
    flags=re.IGNORECASE,
)
logger = logging.getLogger(__name__)
DEFAULT_MCP_OVERRIDES_PATH = Path("overrides/mcp_overrides.json")
DEFAULT_MCP_OVERRIDES_SCHEMA_PATH = Path("overrides/schema.json")


def init_mcp_metrics() -> None:
    """Create OTel metric instruments against the currently configured MeterProvider.

    FastAPI configures the provider before entering the application lifespan,
    where this function is called.
    """
    global _mcp_tool_calls, _mcp_tool_duration
    try:
        from opentelemetry import metrics as otel_metrics
    except ImportError:  # pragma: no cover - optional dependency
        logger.warning("OpenTelemetry metrics API is unavailable; MCP metrics are disabled.")
        return

    meter = otel_metrics.get_meter("i3x_server.mcp")
    _mcp_tool_calls = meter.create_counter(
        "mcp.tool_calls",
        description="Total number of MCP tool invocations",
    )
    _mcp_tool_duration = meter.create_histogram(
        "mcp.tool_duration_seconds",
        description="Duration of MCP tool invocations in seconds",
        unit="s",
    )


def _trace_log_fields(request: Request) -> tuple[str, str]:
    if get_current_span is not None:
        span_context = get_current_span().get_span_context()
        if span_context.is_valid:
            return (f"{span_context.trace_id:032x}", f"{span_context.span_id:016x}")

    traceparent = request.headers.get("traceparent", "")
    match = _TRACEPARENT_PATTERN.match(traceparent)
    if match:
        return (match.group(1).lower(), match.group(2).lower())

    return ("-", "-")


def _resolve_override_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return Path(__file__).resolve().parents[1] / candidate


def _load_json_mapping(path: Path, *, label: str) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8-sig") as file:
            payload = json.load(file)
    except json.JSONDecodeError as exc:
        logger.warning(
            "Invalid JSON in %s at %s:%d:%d (%s). Skipping MCP overrides.",
            label,
            path,
            exc.lineno,
            exc.colno,
            exc.msg,
        )
        return None

    if not isinstance(payload, dict):
        logger.warning("%s must contain a JSON object at %s. Skipping MCP overrides.", label, path)
        return None

    return payload


def _validate_overrides_schema(overrides: Mapping[str, Any], schema_path: Path, override_path: Path) -> bool:
    try:
        from jsonschema import SchemaError, ValidationError, validate
    except ImportError:
        logger.warning(
            "jsonschema dependency is missing; cannot validate MCP overrides at %s. Skipping overrides.",
            override_path,
        )
        return False

    if not schema_path.exists():
        logger.warning(
            "MCP overrides schema file not found at %s. Skipping overrides from %s.",
            schema_path,
            override_path,
        )
        return False

    schema = _load_json_mapping(schema_path, label="MCP overrides schema")
    if schema is None:
        return False

    try:
        validate(instance=overrides, schema=schema)
    except ValidationError as exc:
        location = ".".join(str(part) for part in exc.absolute_path) or "<root>"
        logger.warning(
            "MCP overrides validation failed for %s at %s: %s. Overrides were not applied.",
            override_path,
            location,
            exc.message,
        )
        return False
    except SchemaError as exc:
        logger.warning(
            "MCP overrides schema at %s is invalid (%s). Skipping overrides from %s.",
            schema_path,
            exc.message,
            override_path,
        )
        return False

    return True


def load_overrides(
    path: str | Path = DEFAULT_MCP_OVERRIDES_PATH,
    schema_path: str | Path = DEFAULT_MCP_OVERRIDES_SCHEMA_PATH,
) -> dict[str, Any]:
    override_file = _resolve_override_path(path)
    if not override_file.exists():
        return {}

    overrides = _load_json_mapping(override_file, label="MCP overrides")
    if overrides is None:
        return {}

    schema_file = _resolve_override_path(schema_path)
    if not _validate_overrides_schema(overrides, schema_file, override_file):
        return {}

    return dict(overrides)


def load_tool_overrides(
    path: str | Path = DEFAULT_MCP_OVERRIDES_PATH,
    schema_path: str | Path = DEFAULT_MCP_OVERRIDES_SCHEMA_PATH,
) -> dict[str, Any]:
    overrides = load_overrides(path, schema_path)
    tools = overrides.get("tools")
    if isinstance(tools, Mapping):
        return dict(tools)
    return overrides


def load_prompt_overrides(
    path: str | Path = DEFAULT_MCP_OVERRIDES_PATH,
    schema_path: str | Path = DEFAULT_MCP_OVERRIDES_SCHEMA_PATH,
) -> dict[str, Any]:
    overrides = load_overrides(path, schema_path)
    prompts = overrides.get("prompts")
    if isinstance(prompts, Mapping):
        return dict(prompts)
    return {}


def load_feature_overrides(
    path: str | Path = DEFAULT_MCP_OVERRIDES_PATH,
    schema_path: str | Path = DEFAULT_MCP_OVERRIDES_SCHEMA_PATH,
) -> dict[str, Any]:
    overrides = load_overrides(path, schema_path)
    features = overrides.get("features")
    if isinstance(features, Mapping):
        return dict(features)
    return {}


@dataclass(frozen=True, slots=True)
class McpToolDefinition:
    name: str
    description: str
    method: str
    path: str
    input_schema: dict[str, Any]
    path_parameters: tuple[str, ...]
    query_parameters: tuple[str, ...]
    body_required: bool
    priority: str = "normal"
    keywords: tuple[str, ...] = ()
    server_url_override: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["outputSchema"] = self.output_schema
        result["annotations"] = self.annotations
        return result

    @property
    def output_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"statusCode": {"type": "integer"}, "body": {}},
            "required": ["statusCode", "body"],
            "additionalProperties": False,
        }

    @property
    def annotations(self) -> dict[str, bool]:
        read_only = (
            self.method == "GET"
            or self.path.endswith(
                (
                    "/objects/list",
                    "/objects/related",
                    "/objects/value",
                    "/objects/history",
                    "/objecttypes/query",
                    "/relationshiptypes/query",
                    "/subscriptions/list",
                )
            )
            and self.method == "POST"
        )
        subscription_mutation = "/subscriptions" in self.path and not read_only
        return {
            "readOnlyHint": read_only,
            "destructiveHint": not read_only
            and not (subscription_mutation and self.path.endswith(("/subscriptions", "/register"))),
            "idempotentHint": read_only,
            "openWorldHint": True,
        }


@dataclass(frozen=True, slots=True)
class _InternalASGIResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


def build_mcp_tools(
    openapi_spec: Mapping[str, Any],
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, McpToolDefinition]:
    if overrides is None:
        overrides = load_tool_overrides()

    components = openapi_spec.get("components")
    if not isinstance(components, Mapping):
        components = {}

    paths = openapi_spec.get("paths")
    if not isinstance(paths, Mapping):
        return {}

    tools: dict[str, McpToolDefinition] = {}
    for path, methods in paths.items():
        if not isinstance(path, str) or not isinstance(methods, Mapping):
            continue
        if path.startswith("/mcp"):
            continue
        # Extract per-path server override if present
        path_server_override: str | None = None
        if isinstance(methods.get("servers"), list):
            for server in methods.get("servers", []):
                if isinstance(server, Mapping) and isinstance(server.get("url"), str):
                    path_server_override = server.get("url")
                    break
        for method, details in methods.items():
            if not isinstance(method, str) or not isinstance(details, Mapping):
                continue
            if method.upper() == "PUT":
                continue
            operation_id = details.get("operationId")
            if not isinstance(operation_id, str) or operation_id in MCP_EXCLUDED_OPERATION_IDS:
                continue
            if path.endswith("/subscriptions/stream"):
                continue

            input_properties: dict[str, Any] = {}
            required: list[str] = []
            path_parameters: list[str] = []
            query_parameters: list[str] = []

            for parameter in details.get("parameters", []):
                if not isinstance(parameter, Mapping):
                    continue
                parameter_name = parameter.get("name")
                parameter_location = parameter.get("in")
                if not isinstance(parameter_name, str) or not isinstance(parameter_location, str):
                    continue
                if parameter_location not in {"path", "query"}:
                    continue
                parameter_schema = _resolve_schema(parameter.get("schema", {"type": "string"}), components)
                input_properties[parameter_name] = parameter_schema
                if parameter.get("required"):
                    required.append(parameter_name)
                if parameter_location == "path":
                    path_parameters.append(parameter_name)
                elif parameter_location == "query":
                    query_parameters.append(parameter_name)

            request_body = details.get("requestBody")
            body_required = False
            if isinstance(request_body, Mapping):
                body_required = bool(request_body.get("required"))
                body_schema = _resolve_request_body_schema(request_body, components)
                if body_schema is not None:
                    if operation_id in MCP_BODY_PAGINATED_TOOLS and isinstance(body_schema.get("properties"), dict):
                        body_schema["properties"]["limit"] = {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 500,
                        }
                        body_schema["properties"]["offset"] = {"type": "integer", "minimum": 0}
                    input_properties["body"] = body_schema
                    if body_required:
                        required.append("body")

            if operation_id in MCP_PAGINATED_LIST_TOOLS:
                input_properties["limit"] = {"type": "integer", "minimum": 1, "maximum": 500}
                input_properties["offset"] = {"type": "integer", "minimum": 0}

            override = overrides.get(operation_id, {}) if isinstance(overrides, Mapping) else {}
            if not isinstance(override, Mapping):
                override = {}

            keywords = override.get("keywords", [])
            if not isinstance(keywords, list):
                keywords = []

            tools[operation_id] = McpToolDefinition(
                name=operation_id,
                description=str(
                    override.get("description") or details.get("summary") or details.get("description") or ""
                ),
                method=method.upper(),
                path=path,
                input_schema={
                    "type": "object",
                    "properties": input_properties,
                    "required": required,
                    "additionalProperties": False,
                },
                path_parameters=tuple(path_parameters),
                query_parameters=tuple(query_parameters),
                body_required=body_required,
                priority=str(override.get("priority") or "normal"),
                keywords=tuple(str(keyword) for keyword in keywords if isinstance(keyword, str)),
                server_url_override=path_server_override,
            )

    override = overrides.get(MCP_CONTAINER_VALUES_TOOL, {}) if isinstance(overrides, Mapping) else {}
    if not isinstance(override, Mapping):
        override = {}
    keywords = override.get("keywords", [])
    if not isinstance(keywords, list):
        keywords = []
    tools[MCP_CONTAINER_VALUES_TOOL] = McpToolDefinition(
        name=MCP_CONTAINER_VALUES_TOOL,
        description=str(
            override.get("description")
            or "Read current values for properties under one discovered container. "
            "Pass its elementId; do not build a long elementIds array."
        ),
        method="POST",
        path="/v1/objects/value",
        input_schema={
            "type": "object",
            "properties": {
                "containerId": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 50},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            "required": ["containerId"],
            "additionalProperties": False,
        },
        path_parameters=(),
        query_parameters=(),
        body_required=False,
        priority=str(override.get("priority") or "high"),
        keywords=tuple(str(keyword) for keyword in keywords if isinstance(keyword, str)),
    )
    model_tool_descriptions = {
        "resolveElementIdsToNodeIds": (
            "Resolve cached i3X ElementIds to their OPC UA NodeIds and ExpandedNodeIds. "
            "Uses only the current cached model and never contacts the OPC UA server."
        ),
        "resolveNodeIdsToElementIds": (
            "Resolve standard or expanded OPC UA NodeIds to cached i3X ElementIds. "
            "Uses only the current cached model and never contacts the OPC UA server."
        ),
        "searchModelElements": (
            "Search the cached model by name, ID, type, semantic role, namespace, profile, or relationship. "
            "Returns matching element IDs and compact semantic context."
        ),
        "getElementContext": (
            "Get one cached model element with bounded hierarchy ancestors, direct children, semantic metadata, "
            "and direct relationships."
        ),
        "summarizeSubtree": (
            "Return a bounded tree and kind/role counts for one cached model branch, keeping hierarchy and "
            "composition distinct."
        ),
        "traceRelationshipPath": (
            "Find a shortest bounded path between two cached model elements across selected relationship planes."
        ),
    }
    for name, input_schema in MCP_MODEL_TOOL_SCHEMAS.items():
        tools[name] = McpToolDefinition(
            name=name,
            description=model_tool_descriptions[name],
            method="GET",
            path=f"/mcp/model/{name}",
            input_schema=input_schema,
            path_parameters=(),
            query_parameters=(),
            body_required=False,
            priority=(
                "high"
                if name
                in {
                    "resolveElementIdsToNodeIds",
                    "resolveNodeIdsToElementIds",
                    "searchModelElements",
                    "getElementContext",
                }
                else "normal"
            ),
        )
    return tools


def _paginate_mcp_list(body: Any, *, limit: int, offset: int) -> Any:
    if not isinstance(body, dict) or not isinstance(body.get("result"), list):
        return body
    items = body["result"]
    page = items[offset : offset + limit]
    pagination: dict[str, int] = {
        "total": len(items),
        "returned": len(page),
        "offset": offset,
    }
    if offset + len(page) < len(items):
        pagination["nextOffset"] = offset + len(page)
    return {**body, "result": page, "pagination": pagination}


def _validate_mcp_page(limit: Any, offset: Any) -> None:
    if type(limit) is not int or not 1 <= limit <= 500:
        raise i3x_http_error(400, "Bad Request", "limit must be an integer from 1 to 500")
    if type(offset) is not int or offset < 0:
        raise i3x_http_error(400, "Bad Request", "offset must be a non-negative integer")


def _model_node_summary(model: BuildResult, node: ModelNode) -> dict[str, Any]:
    profile_ids = model.applied_profile_ids_by_id.get(
        node.id,
        node.metadata.get("appliedProfileIds", []),
    )
    if not isinstance(profile_ids, list):
        profile_ids = []
    return {
        "elementId": node.id,
        "displayName": node.name,
        "kind": node.kind,
        "semanticRole": model.semantic_role_by_id.get(node.id, node.semantic_role),
        "mappingConfidence": model.mapping_confidence_by_id.get(node.id, node.mapping_confidence),
        "typeElementId": node.type or node.source_type_id or node.kind,
        "namespaceUri": model.namespace_uri_by_id.get(node.id) or node.metadata.get("namespaceUri"),
        "appliedProfileIds": profile_ids,
        "parentId": node.parent_id,
    }


def _resolve_element_ids_to_node_ids(model: BuildResult, element_ids: list[Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for element_id in element_ids:
        if not isinstance(element_id, str) or not element_id.strip():
            items.append(
                {
                    "success": False,
                    "elementId": element_id,
                    "error": {"code": 400, "message": "ElementId must be a non-empty string"},
                }
            )
            continue
        node = model.nodes_by_id.get(element_id)
        if node is None:
            items.append(
                {
                    "success": False,
                    "elementId": element_id,
                    "error": {"code": 404, "message": "ElementId not found"},
                }
            )
            continue
        node_id = node.metadata.get("uaNodeId")
        expanded_id = node.metadata.get("expandedNodeId")
        if not isinstance(node_id, str) or not node_id:
            items.append(
                {
                    "success": False,
                    "elementId": element_id,
                    "error": {"code": 404, "message": "OPC UA NodeId is not available in the cached model"},
                }
            )
            continue
        result: dict[str, Any] = {
            "success": True,
            "elementId": node.id,
            "nodeId": node_id,
            "name": node.name,
            "kind": node.kind,
        }
        if isinstance(expanded_id, str) and expanded_id.startswith("nsu="):
            result["expandedNodeId"] = expanded_id
        items.append(result)
    return {"success": all(item["success"] for item in items), "items": items}


def _resolve_node_ids_to_element_ids(model: BuildResult, node_ids: list[Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for node_id in node_ids:
        if not isinstance(node_id, str) or not node_id.strip():
            items.append(
                {
                    "success": False,
                    "inputNodeId": node_id,
                    "error": {"code": 400, "message": "NodeId must be a non-empty string"},
                }
            )
            continue
        canonical_id = canonical_node_id(node_id, model.namespace_uri_by_index)
        if canonical_id is None:
            items.append(
                {
                    "success": False,
                    "inputNodeId": node_id,
                    "error": {"code": 400, "message": "Invalid OPC UA NodeId format"},
                }
            )
            continue
        element_id = model.element_id_by_expanded_node_id.get(canonical_id)
        if element_id is None:
            element_id = model.element_id_by_node_id.get(node_id.strip())
        node = model.nodes_by_id.get(element_id) if element_id is not None else None
        if node is None:
            items.append(
                {
                    "success": False,
                    "inputNodeId": node_id,
                    "error": {"code": 404, "message": "NodeId not found in the cached model"},
                }
            )
            continue
        items.append(
            {
                "success": True,
                "inputNodeId": node_id,
                "elementId": node.id,
                "name": node.name,
                "kind": node.kind,
            }
        )
    return {"success": all(item["success"] for item in items), "items": items}


def _model_relationships(model: BuildResult, node: ModelNode) -> dict[str, list[str]]:
    relationships = model.relationships_by_id.get(node.id)
    if relationships is not None:
        return relationships
    return node.relationships


def _relationship_plane(relationship_type: str) -> str:
    if relationship_type in {"HasParent", "HasChildren"}:
        return "hierarchy"
    if relationship_type in {"HasComponent", "ComponentOf"}:
        return "composition"
    return "graph"


def _validate_model_tool_arguments(tool_name: str, arguments: Mapping[str, Any]) -> None:
    required = MCP_MODEL_TOOL_SCHEMAS[tool_name].get("required", [])
    missing = [field for field in required if field not in arguments]
    if missing:
        raise i3x_http_error(400, "Bad Request", f"Missing required arguments: {', '.join(missing)}")

    batch_field = {
        "resolveElementIdsToNodeIds": "elementIds",
        "resolveNodeIdsToElementIds": "nodeIds",
    }.get(tool_name)
    if batch_field is not None:
        values = arguments[batch_field]
        if not isinstance(values, list) or not values or len(values) > MCP_ID_RESOLUTION_MAX_ITEMS:
            raise i3x_http_error(
                400,
                "Bad Request",
                f"{batch_field} must contain between 1 and {MCP_ID_RESOLUTION_MAX_ITEMS} items",
            )
        return

    for field in ("elementId", "rootElementId", "sourceElementId", "targetElementId"):
        if field in arguments and (not isinstance(arguments[field], str) or not arguments[field].strip()):
            raise i3x_http_error(400, "Bad Request", f"{field} must be a non-empty string")
    for field in (
        "text",
        "kind",
        "semanticRole",
        "namespaceUri",
        "typeElementId",
        "mappingConfidence",
        "appliedProfileId",
        "relationshipType",
    ):
        if field in arguments and not isinstance(arguments[field], str):
            raise i3x_http_error(400, "Bad Request", f"{field} must be a string")
    for field in (
        "includeChildren",
        "includeRelationships",
        "includeTypeAndMetadata",
        "includeHierarchy",
        "includeComposition",
        "includeProperties",
        "includeActions",
    ):
        if field in arguments and type(arguments[field]) is not bool:
            raise i3x_http_error(400, "Bad Request", f"{field} must be a boolean")

    bounds = {
        "limit": (1, 100),
        "offset": (0, None),
        "ancestorDepth": (0, 32),
        "childLimit": (1, MCP_MODEL_CONTEXT_MAX_ITEMS),
        "relationshipLimit": (1, MCP_MODEL_CONTEXT_MAX_ITEMS),
        "maxDepth": (0, 32),
        "maxNodes": (1, 500),
    }
    for field, (minimum, maximum) in bounds.items():
        if field not in arguments:
            continue
        value = arguments[field]
        if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
            bound_text = f"{minimum} to {maximum}" if maximum is not None else f"at least {minimum}"
            raise i3x_http_error(400, "Bad Request", f"{field} must be an integer from {bound_text}")

    if "relationshipPlanes" in arguments:
        planes = arguments["relationshipPlanes"]
        if not isinstance(planes, list) or any(
            not isinstance(plane, str) or plane not in {"hierarchy", "composition", "graph"} for plane in planes
        ):
            raise i3x_http_error(400, "Bad Request", "relationshipPlanes must contain hierarchy, composition, or graph")
    if "direction" in arguments and (
        not isinstance(arguments["direction"], str) or arguments["direction"] not in {"outgoing", "incoming", "either"}
    ):
        raise i3x_http_error(400, "Bad Request", "direction must be outgoing, incoming, or either")

    enum_values = {
        "kind": {"asset", "property", "action", "eventSource"},
        "semanticRole": {"asset", "group", "component", "datapoint", "property", "unknown"},
        "mappingConfidence": {"high", "medium", "low"},
    }
    for field, values in enum_values.items():
        if field in arguments and arguments[field] not in values:
            raise i3x_http_error(400, "Bad Request", f"Invalid {field}: {arguments[field]}")


def _search_model_elements(model: BuildResult, arguments: Mapping[str, Any]) -> dict[str, Any]:
    text = arguments.get("text")
    search_text = text.strip().casefold() if isinstance(text, str) else ""
    matched: list[ModelNode] = []
    for node in model.nodes_by_id.values():
        semantic_role = model.semantic_role_by_id.get(node.id, node.semantic_role)
        mapping_confidence = model.mapping_confidence_by_id.get(node.id, node.mapping_confidence)
        namespace_uri = model.namespace_uri_by_id.get(node.id) or node.metadata.get("namespaceUri")
        profile_ids = model.applied_profile_ids_by_id.get(
            node.id,
            node.metadata.get("appliedProfileIds", []),
        )
        relationship_names = _model_relationships(model, node)
        if arguments.get("kind") is not None and node.kind != arguments["kind"]:
            continue
        if arguments.get("semanticRole") is not None and semantic_role != arguments["semanticRole"]:
            continue
        if arguments.get("namespaceUri") is not None and namespace_uri != arguments["namespaceUri"]:
            continue
        if arguments.get("typeElementId") is not None and arguments["typeElementId"] not in {
            node.type,
            node.source_type_id,
        }:
            continue
        if arguments.get("mappingConfidence") is not None and mapping_confidence != arguments["mappingConfidence"]:
            continue
        if arguments.get("appliedProfileId") is not None and arguments["appliedProfileId"] not in profile_ids:
            continue
        if arguments.get("relationshipType") is not None and arguments["relationshipType"] not in relationship_names:
            continue
        if search_text:
            searchable = (
                node.id,
                node.name,
                node.kind,
                node.type or "",
                node.source_node_id,
                node.source_type_id or "",
                str(semantic_role),
                str(mapping_confidence),
                str(namespace_uri or ""),
                str(node.metadata.get("description", "")),
                " ".join(str(profile_id) for profile_id in profile_ids),
                " ".join(relationship_names),
            )
            if not any(search_text in value.casefold() for value in searchable):
                continue
        matched.append(node)

    matched.sort(key=lambda node: (node.name.casefold(), node.id))
    limit = arguments.get("limit", 25)
    offset = arguments.get("offset", 0)
    page = matched[offset : offset + limit]
    return {
        "modelRevision": model_revision(model),
        "total": len(matched),
        "returned": len(page),
        "offset": offset,
        "items": [_model_node_summary(model, node) for node in page],
        **({"nextOffset": offset + len(page)} if offset + len(page) < len(matched) else {}),
    }


def _get_element_context(model: BuildResult, arguments: Mapping[str, Any]) -> dict[str, Any]:
    element_id = arguments["elementId"]
    node = model.nodes_by_id.get(element_id)
    if node is None:
        raise i3x_http_error(404, "Not Found", f"Model element not found: {element_id}")

    ancestors: list[dict[str, Any]] = []
    current_id = element_id
    visited = {element_id}
    ancestor_depth = arguments.get("ancestorDepth", 3)
    for _ in range(ancestor_depth):
        parent_id = model.hierarchy_parent_by_id.get(current_id)
        if parent_id is None:
            break
        parent = model.nodes_by_id.get(parent_id)
        if parent is None or parent_id in visited:
            break
        ancestors.append(
            {
                "relationshipType": "HasParent",
                "plane": "hierarchy",
                "node": _model_node_summary(model, parent),
            }
        )
        visited.add(parent_id)
        current_id = parent_id

    result: dict[str, Any] = {
        "modelRevision": model_revision(model),
        "element": _model_node_summary(model, node),
        "ancestors": ancestors,
    }
    parents: list[dict[str, Any]] = []
    hierarchy_parent_id = model.hierarchy_parent_by_id.get(node.id)
    if hierarchy_parent_id in model.nodes_by_id:
        parents.append(
            {
                "relationshipType": "HasParent",
                "plane": "hierarchy",
                "node": _model_node_summary(model, model.nodes_by_id[hierarchy_parent_id]),
            }
        )
    composition_parent_id = model.composition_parent_by_id.get(node.id)
    if composition_parent_id in model.nodes_by_id:
        parents.append(
            {
                "relationshipType": "ComponentOf",
                "plane": "composition",
                "node": _model_node_summary(model, model.nodes_by_id[composition_parent_id]),
            }
        )
    if parents:
        result["parents"] = parents
    if arguments.get("includeChildren", True):
        hierarchy_children = model.hierarchy_children_by_id.get(node.id, [])
        composition_children = model.composition_children_by_id.get(node.id, [])
        child_limit = arguments.get("childLimit", 100)
        result["children"] = {
            "hierarchy": _bounded_context_items(model, hierarchy_children, child_limit),
            "composition": _bounded_context_items(model, composition_children, child_limit),
        }
    if arguments.get("includeRelationships", True):
        relationships = []
        for relationship_type, target_ids in sorted(_model_relationships(model, node).items()):
            for target_id in target_ids:
                target = model.nodes_by_id.get(target_id)
                relationships.append(
                    {
                        "relationshipType": relationship_type,
                        "plane": _relationship_plane(relationship_type),
                        "target": (
                            _model_node_summary(model, target) if target is not None else {"elementId": target_id}
                        ),
                    }
                )
        relationship_limit = arguments.get("relationshipLimit", 100)
        result["relationships"] = {
            "total": len(relationships),
            "returned": min(len(relationships), relationship_limit),
            "truncated": len(relationships) > relationship_limit,
            "items": relationships[:relationship_limit],
        }
    if arguments.get("includeTypeAndMetadata", True):
        result["source"] = {
            "sourceNodeId": node.source_node_id,
            "sourceTypeId": node.source_type_id,
            "description": node.metadata.get("description"),
            "namespaceUri": model.namespace_uri_by_id.get(node.id) or node.metadata.get("namespaceUri"),
            "appliedProfileIds": model.applied_profile_ids_by_id.get(
                node.id,
                node.metadata.get("appliedProfileIds", []),
            ),
            "extendedAttributes": node.metadata,
        }
    return result


def _bounded_context_items(model: BuildResult, element_ids: list[str], limit: int) -> dict[str, Any]:
    items = [
        _model_node_summary(model, model.nodes_by_id[element_id])
        for element_id in element_ids
        if element_id in model.nodes_by_id
    ]
    return {
        "total": len(items),
        "returned": min(len(items), limit),
        "truncated": len(items) > limit,
        "items": items[:limit],
    }


def _summarize_subtree(model: BuildResult, arguments: Mapping[str, Any]) -> dict[str, Any]:
    root_id = arguments["rootElementId"]
    root = model.nodes_by_id.get(root_id)
    if root is None:
        raise i3x_http_error(404, "Not Found", f"Model element not found: {root_id}")

    max_depth = arguments.get("maxDepth", 4)
    max_nodes = arguments.get("maxNodes", 200)
    include_hierarchy = arguments.get("includeHierarchy", True)
    include_composition = arguments.get("includeComposition", True)
    include_properties = arguments.get("includeProperties", True)
    include_actions = arguments.get("includeActions", True)
    included: dict[str, dict[str, Any]] = {}
    kind_counts: dict[str, int] = {}
    role_counts: dict[str, int] = {}
    truncated = False

    def visit(node: ModelNode, depth: int, path: set[str]) -> dict[str, Any]:
        nonlocal truncated
        summary = _model_node_summary(model, node)
        included[node.id] = summary
        kind_counts[node.kind] = kind_counts.get(node.kind, 0) + 1
        role = str(summary["semanticRole"])
        role_counts[role] = role_counts.get(role, 0) + 1
        children: list[dict[str, Any]] = []
        summary["children"] = children
        if depth >= max_depth:
            if _subtree_child_edges(model, node, include_hierarchy, include_composition):
                truncated = True
            return summary
        for relationship_type, child_id in _subtree_child_edges(model, node, include_hierarchy, include_composition):
            child = model.nodes_by_id.get(child_id)
            if child is None or child_id in path:
                continue
            if (child.kind == "property" and not include_properties) or (
                child.kind == "action" and not include_actions
            ):
                continue
            if len(included) >= max_nodes:
                truncated = True
                break
            children.append(
                {
                    "relationshipType": relationship_type,
                    "node": visit(child, depth + 1, path | {child_id}),
                }
            )
        return summary

    tree = visit(root, 0, {root_id})
    return {
        "modelRevision": model_revision(model),
        "rootElementId": root_id,
        "totalNodes": len(included),
        "truncated": truncated,
        "tree": tree,
        "counts": {"byKind": kind_counts, "bySemanticRole": role_counts},
    }


def _subtree_child_edges(
    model: BuildResult,
    node: ModelNode,
    include_hierarchy: bool,
    include_composition: bool,
) -> list[tuple[str, str]]:
    edges: list[tuple[str, str]] = []
    if include_hierarchy:
        edges.extend(("HasChildren", child_id) for child_id in model.hierarchy_children_by_id.get(node.id, []))
    if include_composition:
        edges.extend(("HasComponent", child_id) for child_id in model.composition_children_by_id.get(node.id, []))
    return edges


def _trace_relationship_path(model: BuildResult, arguments: Mapping[str, Any]) -> dict[str, Any]:
    source_id = arguments["sourceElementId"]
    target_id = arguments["targetElementId"]
    for element_id in (source_id, target_id):
        if element_id not in model.nodes_by_id:
            raise i3x_http_error(404, "Not Found", f"Model element not found: {element_id}")

    planes = set(arguments.get("relationshipPlanes", ["hierarchy", "composition", "graph"]))
    direction = arguments.get("direction", "either")
    max_depth = arguments.get("maxDepth", 8)

    outgoing_by_id: dict[str, set[tuple[str, str, str]]] = {element_id: set() for element_id in model.nodes_by_id}
    incoming_by_id: dict[str, set[tuple[str, str, str]]] = {element_id: set() for element_id in model.nodes_by_id}
    for relationship_source_id, source in model.nodes_by_id.items():
        for relationship_type, targets in _model_relationships(model, source).items():
            plane = _relationship_plane(relationship_type)
            if plane not in planes:
                continue
            for target_element_id in targets:
                if target_element_id not in model.nodes_by_id:
                    continue
                outgoing_by_id[relationship_source_id].add((target_element_id, relationship_type, plane))
                incoming_by_id[target_element_id].add((relationship_source_id, relationship_type, plane))

    def neighbors(element_id: str) -> list[tuple[str, str, str, str]]:
        found: set[tuple[str, str, str, str]] = set()
        if direction in {"outgoing", "either"}:
            found.update(
                (neighbor_id, relationship_type, plane, "outgoing")
                for neighbor_id, relationship_type, plane in outgoing_by_id[element_id]
            )
        if direction in {"incoming", "either"}:
            found.update(
                (neighbor_id, relationship_type, plane, "incoming")
                for neighbor_id, relationship_type, plane in incoming_by_id[element_id]
            )
        plane_priority = {"hierarchy": 0, "composition": 1, "graph": 2}
        return sorted(found, key=lambda item: (item[0], plane_priority[item[2]], item[1], item[3]))

    queue = deque([(source_id, 0, [{"element": _model_node_summary(model, model.nodes_by_id[source_id])}])])
    visited = {source_id}
    truncated = False
    while queue:
        current_id, depth, path = queue.popleft()
        if current_id == target_id:
            return {
                "modelRevision": model_revision(model),
                "found": True,
                "truncated": False,
                "pathLength": depth,
                "path": path,
            }
        adjacent = neighbors(current_id)
        if depth >= max_depth:
            if any(neighbor_id not in visited for neighbor_id, _, _, _ in adjacent):
                truncated = True
            continue
        for neighbor_id, relationship_type, plane, traversal_direction in adjacent:
            if neighbor_id in visited:
                continue
            visited.add(neighbor_id)
            next_path = [
                *path,
                {
                    "relationship": {
                        "relationshipType": relationship_type,
                        "plane": plane,
                        "direction": traversal_direction,
                    }
                },
                {"element": _model_node_summary(model, model.nodes_by_id[neighbor_id])},
            ]
            if neighbor_id == target_id:
                return {
                    "modelRevision": model_revision(model),
                    "found": True,
                    "truncated": False,
                    "pathLength": depth + 1,
                    "path": next_path,
                }
            queue.append((neighbor_id, depth + 1, next_path))
            if len(visited) >= MCP_MODEL_PATH_MAX_NODES:
                truncated = bool(queue)
                queue.clear()
                break
    return {
        "modelRevision": model_revision(model),
        "found": False,
        "truncated": truncated,
        "pathLength": None,
        "path": [],
    }


async def _invoke_model_tool(
    request: Request,
    tool_name: str,
    arguments: Mapping[str, Any],
) -> _InternalASGIResponse:
    _validate_model_tool_arguments(tool_name, arguments)
    model_provider = getattr(request.app.state, "mcp_model_provider", None)
    if not callable(model_provider):
        raise i3x_http_error(500, "Internal Error", "MCP model provider is not initialized")
    model: BuildResult = await model_provider(request)
    if tool_name == "resolveElementIdsToNodeIds":
        body = _resolve_element_ids_to_node_ids(model, arguments["elementIds"])
    elif tool_name == "resolveNodeIdsToElementIds":
        body = _resolve_node_ids_to_element_ids(model, arguments["nodeIds"])
    else:
        handlers = {
            "searchModelElements": _search_model_elements,
            "getElementContext": _get_element_context,
            "summarizeSubtree": _summarize_subtree,
            "traceRelationshipPath": _trace_relationship_path,
        }
        body = handlers[tool_name](model, arguments)
    return _InternalASGIResponse(
        status_code=200,
        headers={"content-type": "application/json"},
        body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
    )


async def _invoke_cached_identity_tool(
    request: Request,
    tool_name: str,
    arguments: Mapping[str, Any],
) -> _InternalASGIResponse:
    _validate_model_tool_arguments(tool_name, arguments)
    model = getattr(request.app.state, "model_cache", None)
    if not isinstance(model, BuildResult):
        raise i3x_http_error(503, "Service Unavailable", "Cached model is not available")
    if tool_name == "resolveElementIdsToNodeIds":
        body = _resolve_element_ids_to_node_ids(model, arguments["elementIds"])
    else:
        body = _resolve_node_ids_to_element_ids(model, arguments["nodeIds"])
    return _InternalASGIResponse(
        status_code=200,
        headers={"content-type": "application/json"},
        body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
    )


async def _invoke_container_values(
    request: Request,
    *,
    container_id: str,
    limit: int,
    offset: int,
) -> _InternalASGIResponse:
    if not isinstance(container_id, str) or not container_id.strip():
        raise i3x_http_error(400, "Bad Request", "containerId must not be empty")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise i3x_http_error(400, "Bad Request", "limit must be an integer from 1 to 50")
    if type(offset) is not int or offset < 0:
        raise i3x_http_error(400, "Bad Request", "offset must be a non-negative integer")

    model_provider = getattr(request.app.state, "mcp_model_provider", None)
    if not callable(model_provider):
        raise i3x_http_error(500, "Internal Error", "MCP model provider is not initialized")
    model: BuildResult = await model_provider(request)
    container = model.nodes_by_id.get(container_id)
    if container is None:
        raise i3x_http_error(404, "Not Found", f"Container not found: {container_id}")
    if container.kind == "property":
        raise i3x_http_error(400, "Bad Request", "containerId must identify an object, not a property")

    properties: list[ModelNode] = []
    visited: set[str] = set()
    pending = [(container_id, 0)]
    while pending:
        element_id, depth = pending.pop()
        if element_id in visited:
            continue
        visited.add(element_id)
        if len(visited) > MCP_CONTAINER_VALUES_MAX_NODES:
            raise i3x_http_error(
                413,
                "Content Too Large",
                f"Container subtree exceeds {MCP_CONTAINER_VALUES_MAX_NODES} nodes",
            )
        if depth > MCP_CONTAINER_VALUES_MAX_DEPTH:
            raise i3x_http_error(
                413,
                "Content Too Large",
                f"Container subtree exceeds depth {MCP_CONTAINER_VALUES_MAX_DEPTH}",
            )
        node = model.nodes_by_id.get(element_id)
        if node is None:
            continue
        if node.kind == "property":
            properties.append(node)
            continue
        pending.extend((child_id, depth + 1) for child_id in reversed(model.children_by_id.get(element_id, [])))

    total = len(properties)
    page = properties[offset : offset + limit]
    values_by_id: dict[str, Any] = {}
    api_prefix = getattr(request.app.state, "mcp_api_prefix", "")
    if not isinstance(api_prefix, str):
        api_prefix = ""
    value_path = _safe_request_path(api_prefix, "/v1/objects/value")

    for start in range(0, len(page), MCP_CONTAINER_VALUES_BATCH_SIZE):
        batch = page[start : start + MCP_CONTAINER_VALUES_BATCH_SIZE]
        response = await _invoke_internal_asgi(
            app=request.app,
            method="POST",
            path=value_path,
            query_params={},
            request_body={"elementIds": [node.id for node in batch], "maxDepth": 1},
        )
        if response.status_code >= 400:
            return response
        try:
            body = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise i3x_http_error(502, "Bad Gateway", "Value endpoint returned invalid JSON") from exc
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            raise i3x_http_error(502, "Bad Gateway", "Value endpoint returned an invalid result")
        for result in results:
            if isinstance(result, dict) and isinstance(result.get("elementId"), str):
                values_by_id[result["elementId"]] = result

    items: list[dict[str, Any]] = []
    for node in page:
        result = values_by_id.get(node.id)
        if result is None:
            items.append(
                {
                    "id": node.id,
                    "name": node.name,
                    "success": False,
                    "error": {"message": "Value endpoint returned no result for this property"},
                }
            )
        elif result.get("success") is True:
            vqt = result.get("result")
            compact_vqt = (
                {key: vqt[key] for key in ("value", "quality", "timestamp") if key in vqt}
                if isinstance(vqt, dict)
                else vqt
            )
            items.append(
                {
                    "id": node.id,
                    "name": node.name,
                    "success": True,
                    "value": compact_vqt,
                }
            )
        else:
            items.append(
                {
                    "id": node.id,
                    "name": node.name,
                    "success": False,
                    "error": result.get("error"),
                    "responseDetail": result.get("responseDetail"),
                }
            )

    response_body: dict[str, Any] = {
        "success": all(item["success"] for item in items),
        "containerId": container_id,
        "total": total,
        "returned": len(items),
        "offset": offset,
        "items": items,
    }
    if offset + len(page) < total:
        response_body["nextOffset"] = offset + len(page)
    return _InternalASGIResponse(
        status_code=200,
        headers={"content-type": "application/json"},
        body=json.dumps(response_body).encode("utf-8"),
    )


def _pagination(total: int, returned: int, offset: int) -> dict[str, int]:
    page = {"total": total, "returned": returned, "offset": offset}
    if offset + returned < total:
        page["nextOffset"] = offset + returned
    return page


def _paginate_mcp_related(body: Any, *, limit: int, offset: int) -> Any:
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        return body
    results = []
    for item in body["results"]:
        if not isinstance(item, dict) or not item.get("success") or not isinstance(item.get("result"), list):
            results.append(item)
            continue
        related = item["result"]
        results.append(
            {
                **item,
                "result": related[offset : offset + limit],
                "pagination": _pagination(len(related), len(related[offset : offset + limit]), offset),
            }
        )
    return {**body, "results": results}


def _paginate_history_result(result: Any, *, limit: int, offset: int) -> Any:
    if not isinstance(result, dict):
        return result
    paged = dict(result)
    values = result.get("values")
    if isinstance(values, list):
        selected = values[offset : offset + limit]
        paged["values"] = selected
        paged["pagination"] = _pagination(len(values), len(selected), offset)
    components = result.get("components")
    if isinstance(components, dict):
        paged["components"] = {
            key: _paginate_history_result(value, limit=limit, offset=offset) for key, value in components.items()
        }
    return paged


def _paginate_mcp_history(body: Any, *, limit: int, offset: int) -> Any:
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        return body
    results = []
    for item in body["results"]:
        if isinstance(item, dict) and item.get("success") and isinstance(item.get("result"), dict):
            results.append({**item, "result": _paginate_history_result(item["result"], limit=limit, offset=offset)})
        else:
            results.append(item)
    return {**body, "results": results}


def get_api_prefix(openapi_spec: Mapping[str, Any]) -> str:
    servers = openapi_spec.get("servers")
    if isinstance(servers, list):
        for server in servers:
            if not isinstance(server, Mapping):
                continue
            url = server.get("url")
            if isinstance(url, str) and url:
                return _safe_api_prefix(url)
    return ""


def _safe_api_prefix(api_prefix: str) -> str:
    parsed = urlsplit(api_prefix)
    if parsed.scheme or parsed.netloc:
        api_prefix = parsed.path
    if not api_prefix:
        return ""
    prefix = api_prefix.strip()
    if not prefix:
        return ""
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    if prefix.startswith("//"):
        raise i3x_http_error(500, "Internal Error", "Invalid MCP API prefix")
    normalized = normpath(prefix)
    if normalized in {".", "/", ""}:
        return ""
    if not normalized.startswith("/") or normalized.startswith("//"):
        raise i3x_http_error(500, "Internal Error", "Invalid MCP API prefix")
    return normalized.rstrip("/")


def _safe_request_path(api_prefix: str, tool_path: str) -> str:
    prefix = _safe_api_prefix(api_prefix)
    if not tool_path.startswith("/"):
        raise i3x_http_error(500, "Internal Error", "Invalid MCP tool path")
    combined = f"{prefix}{tool_path}" if prefix else tool_path
    parsed = urlsplit(combined)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise i3x_http_error(500, "Internal Error", "Invalid MCP request path")
    if not parsed.path.startswith("/") or parsed.path.startswith("//"):
        raise i3x_http_error(500, "Internal Error", "Invalid MCP request path")
    normalized = normpath(parsed.path)
    if not normalized.startswith("/") or normalized.startswith("//"):
        raise i3x_http_error(500, "Internal Error", "Invalid MCP request path")
    if normalized == "/.." or normalized.startswith("/../"):
        raise i3x_http_error(500, "Internal Error", "Invalid MCP request path")
    return normalized


def _resolve_request_body_schema(
    request_body: Mapping[str, Any],
    components: Mapping[str, Any],
) -> dict[str, Any] | None:
    content = request_body.get("content")
    if not isinstance(content, Mapping):
        return None
    json_content = content.get("application/json")
    if not isinstance(json_content, Mapping):
        return None
    schema = json_content.get("schema")
    if schema is None:
        return None
    resolved = _resolve_schema(schema, components)
    return resolved if isinstance(resolved, dict) else None


def _resolve_schema(schema: Any, components: Mapping[str, Any]) -> Any:
    if not isinstance(schema, Mapping):
        return deepcopy(schema)

    if "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str):
            return deepcopy(dict(schema))
        resolved = _resolve_ref(ref, components)
        return _resolve_schema(resolved, components)

    resolved_schema = deepcopy(dict(schema))
    for key in ("allOf", "anyOf", "oneOf"):
        value = resolved_schema.get(key)
        if isinstance(value, list):
            resolved_schema[key] = [_resolve_schema(item, components) for item in value]

    properties = resolved_schema.get("properties")
    if isinstance(properties, Mapping):
        resolved_schema["properties"] = {
            str(name): _resolve_schema(value, components) for name, value in properties.items()
        }

    items = resolved_schema.get("items")
    if items is not None:
        resolved_schema["items"] = _resolve_schema(items, components)

    return resolved_schema


def _resolve_ref(ref: str, components: Mapping[str, Any]) -> Any:
    prefix = "#/components/schemas/"
    if not ref.startswith(prefix):
        return {"$ref": ref}
    schema_name = ref.removeprefix(prefix)
    schemas = components.get("schemas")
    if isinstance(schemas, Mapping) and schema_name in schemas:
        return schemas[schema_name]
    return {"$ref": ref}


def _safe_path_parameter_value(parameter_name: str, value: Any) -> str:
    value_str = str(value)
    parsed = urlsplit(value_str)
    if (
        parsed.scheme
        or parsed.netloc
        or "/" in value_str
        or "\\" in value_str
        or ".." in value_str
        or "?" in value_str
        or "#" in value_str
        or value_str.strip() != value_str
    ):
        raise i3x_http_error(400, "Bad Request", f"Invalid path parameter: {parameter_name}")
    return value_str


def _safe_internal_request_url(path: str) -> httpx.URL:
    safe_path = _safe_request_path("", path)
    request_url = _MCP_INTERNAL_BASE_URL.join(safe_path)
    if request_url.scheme != "http" or request_url.host != "mcp.local":
        raise i3x_http_error(500, "Internal Error", "Invalid MCP request URL")
    return request_url


async def call_mcp_tool(request: Request, tool: McpToolDefinition, arguments: Mapping[str, Any]) -> Response:
    payload = await invoke_mcp_tool(request, tool, arguments)
    return _payload_to_response(payload)


async def invoke_mcp_tool(request: Request, tool: McpToolDefinition, arguments: Mapping[str, Any]) -> Any:
    started = perf_counter()
    trace_id, span_id = _trace_log_fields(request)
    logger.info(
        "MCP tool call started tool=%s method=%s path=%s arg_keys=%s trace_id=%s span_id=%s",
        tool.name,
        tool.method,
        tool.path,
        sorted(arguments.keys()),
        trace_id,
        span_id,
    )
    span_ctx = (
        _mcp_tracer.start_as_current_span(
            "mcp.tool_call",
            attributes={
                "mcp.tool.name": tool.name,
                "mcp.tool.method": tool.method,
                "mcp.tool.path": tool.path,
                "mcp.tool.arg_count": len(arguments),
            },
        )
        if _mcp_tracer is not None
        else _nullcontext()
    )
    with span_ctx as _span:
        try:
            allowed_arguments = set(tool.path_parameters) | set(tool.query_parameters)
            if tool.name in MCP_PAGINATED_LIST_TOOLS:
                allowed_arguments.update({"limit", "offset"})
            if tool.name == MCP_CONTAINER_VALUES_TOOL:
                allowed_arguments.update({"containerId", "limit", "offset"})
            if tool.name in MCP_MODEL_TOOL_SCHEMAS:
                allowed_arguments.update(MCP_MODEL_TOOL_SCHEMAS[tool.name].get("properties", {}))
            if tool.body_required or "body" in tool.input_schema.get("properties", {}):
                allowed_arguments.add("body")

            required_fields = set(tool.input_schema.get("required", []))

            unexpected_arguments = sorted(set(arguments) - allowed_arguments)
            if unexpected_arguments:
                raise i3x_http_error(400, "Bad Request", f"Unexpected arguments: {', '.join(unexpected_arguments)}")

            missing_path_parameters = [name for name in tool.path_parameters if name not in arguments]
            missing_required_query_parameters = [
                name for name in tool.query_parameters if name in required_fields and name not in arguments
            ]
            missing_arguments = missing_path_parameters + missing_required_query_parameters
            if missing_arguments:
                raise i3x_http_error(400, "Bad Request", f"Missing required arguments: {', '.join(missing_arguments)}")
            if tool.name in MCP_MODEL_TOOL_SCHEMAS:
                _validate_model_tool_arguments(tool.name, arguments)
            if tool.name in MCP_PAGINATED_LIST_TOOLS:
                limit = arguments.get("limit", MCP_DEFAULT_PAGE_SIZE)
                offset = arguments.get("offset", 0)
                if type(limit) is not int or not 1 <= limit <= 500:
                    raise i3x_http_error(400, "Bad Request", "limit must be an integer from 1 to 500")
                if type(offset) is not int or offset < 0:
                    raise i3x_http_error(400, "Bad Request", "offset must be a non-negative integer")

            api_prefix = getattr(request.app.state, "mcp_api_prefix", "")
            if not isinstance(api_prefix, str):
                api_prefix = ""

            # Use per-path server override if present, otherwise use global api_prefix
            if tool.server_url_override is not None:
                effective_prefix = _safe_api_prefix(tool.server_url_override)
            else:
                effective_prefix = api_prefix

            resolved_path = _safe_request_path(effective_prefix, tool.path)
            for parameter_name in tool.path_parameters:
                placeholder = "{" + parameter_name + "}"
                if placeholder not in resolved_path:
                    raise i3x_http_error(500, "Internal Error", f"Path placeholder not found for {parameter_name}")
                safe_value = _safe_path_parameter_value(parameter_name, arguments[parameter_name])
                resolved_path = resolved_path.replace(placeholder, quote(safe_value, safe=""))

            request_target = _safe_request_path("", resolved_path)

            query_params = {
                parameter_name: arguments[parameter_name]
                for parameter_name in tool.query_parameters
                if arguments.get(parameter_name) is not None
            }
            if tool.name == "getObjectsV1" and "root" in tool.query_parameters:
                query_params.setdefault("root", True)

            request_body = arguments.get("body") if "body" in arguments else None
            body_page_size: int | None = None
            body_page_offset = 0
            if tool.name in MCP_BODY_PAGINATED_TOOLS and isinstance(request_body, dict):
                request_body = dict(request_body)
                default_limit = MCP_BODY_PAGINATED_TOOLS[tool.name]
                body_page_size = request_body.pop("limit", default_limit)
                body_page_offset = request_body.pop("offset", 0)
                _validate_mcp_page(body_page_size, body_page_offset)
            if tool.body_required and request_body is None:
                raise i3x_http_error(400, "Bad Request", "Missing required body")

            if tool.name == MCP_CONTAINER_VALUES_TOOL:
                response = await _invoke_container_values(
                    request,
                    container_id=arguments.get("containerId", ""),
                    limit=arguments.get("limit", 50),
                    offset=arguments.get("offset", 0),
                )
            elif tool.name in {"resolveElementIdsToNodeIds", "resolveNodeIdsToElementIds"}:
                response = await _invoke_cached_identity_tool(request, tool.name, arguments)
            elif tool.name in MCP_MODEL_TOOL_SCHEMAS:
                response = await _invoke_model_tool(request, tool.name, arguments)
            else:
                response = await _invoke_internal_asgi(
                    app=request.app,
                    method=tool.method,
                    path=request_target,
                    query_params=query_params,
                    request_body=request_body,
                )

            if response.headers.get("content-type", "").startswith("application/json"):
                try:
                    body = json.loads(response.body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise i3x_http_error(502, "Bad Gateway", "Upstream response was not valid JSON") from exc
            else:
                body = {
                    "text": response.body.decode("utf-8", errors="replace"),
                    "content_type": response.headers.get("content-type"),
                }

            if tool.name in MCP_PAGINATED_LIST_TOOLS:
                body = _paginate_mcp_list(
                    body,
                    limit=arguments.get("limit", MCP_DEFAULT_PAGE_SIZE),
                    offset=arguments.get("offset", 0),
                )
            elif tool.name == "queryRelatedObjectsV1" and body_page_size is not None:
                body = _paginate_mcp_related(body, limit=body_page_size, offset=body_page_offset)
            elif tool.name == "queryHistoricalValuesV1" and body_page_size is not None:
                body = _paginate_mcp_history(body, limit=body_page_size, offset=body_page_offset)

            duration_s = perf_counter() - started
            if _span is not None:
                _span.set_attribute("http.response.status_code", response.status_code)
                _span.set_attribute("mcp.tool.duration_s", duration_s)
            if _mcp_tool_calls is not None:
                _mcp_tool_calls.add(
                    1,
                    {"mcp.tool.name": tool.name, "http.response.status_code": str(response.status_code)},
                )
            if _mcp_tool_duration is not None:
                _mcp_tool_duration.record(duration_s, {"mcp.tool.name": tool.name})
            logger.info(
                "MCP tool call finished tool=%s method=%s path=%s status=%d duration_s=%.3f trace_id=%s span_id=%s",
                tool.name,
                tool.method,
                request_target,
                response.status_code,
                duration_s,
                trace_id,
                span_id,
            )
            return {"status_code": response.status_code, "body": body}
        except Exception as _exc:
            if _span is not None:
                _span.record_exception(_exc)
                if _OtelStatus is not None and _OtelStatusCode is not None:
                    _span.set_status(_OtelStatus(_OtelStatusCode.ERROR))
            if _mcp_tool_calls is not None:
                _mcp_tool_calls.add(1, {"mcp.tool.name": tool.name, "error": "true"})
            logger.exception(
                "MCP tool call failed tool=%s method=%s path=%s duration_s=%.3f trace_id=%s span_id=%s",
                tool.name,
                tool.method,
                tool.path,
                perf_counter() - started,
                trace_id,
                span_id,
            )
            raise
        raise


async def _invoke_internal_asgi(
    app: Any,
    method: str,
    path: str,
    query_params: Mapping[str, Any],
    request_body: Any,
) -> _InternalASGIResponse:
    query_string = str(httpx.QueryParams(query_params)).encode("ascii") if query_params else b""
    body_bytes = b""
    headers: list[tuple[bytes, bytes]] = [(b"host", b"mcp.local"), (b"accept", b"application/json")]
    if request_body is not None:
        body_bytes = json.dumps(request_body).encode("utf-8")
        headers.append((b"content-type", b"application/json"))
        headers.append((b"content-length", str(len(body_bytes)).encode("ascii")))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": query_string,
        "headers": headers,
        "client": ("127.0.0.1", 0),
        "server": ("mcp.local", 80),
        "root_path": "",
        "app": app,
        "state": {},
    }

    request_sent = False
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if request_sent:
            return {"type": "http.disconnect"}
        request_sent = True
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)

    status_code = 500
    response_headers: dict[str, str] = {}
    response_chunks: list[bytes] = []

    for message in messages:
        if message.get("type") == "http.response.start":
            status_code = int(message.get("status", 500))
            raw_headers = message.get("headers", [])
            for key, value in raw_headers:
                response_headers[key.decode("latin-1").lower()] = value.decode("latin-1")
        elif message.get("type") == "http.response.body":
            response_chunks.append(message.get("body", b""))

    return _InternalASGIResponse(
        status_code=status_code,
        headers=response_headers,
        body=b"".join(response_chunks),
    )


def _payload_to_response(payload: Any) -> Response:
    if isinstance(payload, dict) and "status_code" in payload and "body" in payload:
        body = payload["body"]
        status_code = int(payload["status_code"])
        if isinstance(body, dict) and "text" in body and "content_type" in body:
            return Response(content=str(body["text"]), status_code=status_code, media_type=body.get("content_type"))
        return JSONResponse(status_code=status_code, content=body)
    return JSONResponse(status_code=200, content=payload)
