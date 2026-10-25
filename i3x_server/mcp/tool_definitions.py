from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from i3x_server.mcp.model_access import MCP_MODEL_ACCESS_SCHEMAS
from i3x_server.mcp.model_tools import MCP_MODEL_TOOL_SCHEMAS

logger = logging.getLogger(__name__)
DEFAULT_MCP_OVERRIDES_PATH = Path("overrides/mcp_overrides.json")
DEFAULT_MCP_OVERRIDES_SCHEMA_PATH = Path("overrides/schema.json")

MCP_EXCLUDED_OPERATION_IDS = {"streamSubscription"}
MCP_PAGINATED_LIST_TOOLS = {"getObjectsV1", "getObjectTypesV1", "getRelationshipTypesV1"}
MCP_DEFAULT_PAGE_SIZE = 50
MCP_BODY_PAGINATED_TOOLS = {
    "queryRelatedObjectsV1": 50,
    "queryHistoricalValuesV1": 100,
}

MCP_CONTAINER_VALUES_TOOL = "readContainerValues"
MCP_RESOURCE_LIST_TOOL = "listMcpResources"
MCP_RESOURCE_READ_TOOL = "readMcpResource"
MCP_CONTAINER_VALUES_BATCH_SIZE = 10
MCP_CONTAINER_VALUES_MAX_NODES = 5000
MCP_CONTAINER_VALUES_MAX_DEPTH = 32


def _resolve_override_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return Path(__file__).resolve().parents[2] / candidate


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
            or self.name in {MCP_RESOURCE_LIST_TOOL, MCP_RESOURCE_READ_TOOL}
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
            "openWorldHint": self.name not in MCP_MODEL_ACCESS_SCHEMAS,
        }


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
    tools[MCP_RESOURCE_READ_TOOL] = McpToolDefinition(
        name=MCP_RESOURCE_READ_TOOL,
        description=(
            "Read an MCP resource by its exact URI when the client cannot call MCP resources/read directly. "
            "Useful resources include i3x://guides/opcua-i3x-mapping, i3x://guides/opcua-analysis, "
            "and i3x://guides/i3x-model-analysis. Only resources exposed by this server can be read."
        ),
        method="POST",
        path="/mcp/resources/read",
        input_schema={
            "type": "object",
            "properties": {"uri": {"type": "string", "minLength": 1}},
            "required": ["uri"],
            "additionalProperties": False,
        },
        path_parameters=(),
        query_parameters=(),
        body_required=False,
        priority="high",
    )
    tools[MCP_RESOURCE_LIST_TOOL] = McpToolDefinition(
        name=MCP_RESOURCE_LIST_TOOL,
        description=(
            "List MCP resources exposed by this server when the client cannot call MCP resources/list directly. "
            "Use the returned exact URI with readMcpResource to read a resource."
        ),
        method="GET",
        path="/mcp/resources",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        path_parameters=(),
        query_parameters=(),
        body_required=False,
        priority="high",
    )
    model_tool_descriptions = {
        "resolveBrowsePath": (
            "Resolve an exact root-inclusive display-name path in the cached i3X-mapped model to one element. "
            "Pass path or segments (for names containing '/'). Duplicate paths return 409. No live OPC UA browse."
        ),
        "getBrowsePath": (
            "Get a discovered element's canonical root-inclusive display-name path and segments from cached "
            "hierarchy/composition parents. Use resolveElementIdsToNodeIds for its OPC UA source NodeId. "
            "This is a cached mapping, not a live browse."
        ),
        "getInstancesOfType": (
            "List instances of a discovered typeElementId in the current cached i3X model, "
            "including subtypes by default. "
            "Requires the revision-matched cached type context; never browses OPC UA. "
            "Returns paginated [elementId, name] pairs; this is not a live query of every remote server instance."
        ),
        "getChildren": (
            "List direct children of one discovered element in the cached i3X model. Choose hierarchy (default) "
            "for hierarchy children, composition for components, or graph for outgoing mapped relationships. "
            "Returns paginated [i3X ElementId, display name] pairs only; use getElementContext for metadata or "
            "resolveElementIdsToNodeIds for source NodeIds. No live OPC UA browse."
        ),
        "getCompositionProperties": (
            "List recursive descendant properties by composition only beneath up to 50 discovered i3X object "
            "ElementIds; this does not traverse hierarchy or read values. offset/limit apply independently to each "
            "object. Results cover the cached mapped model, not necessarily every remote variable."
        ),
        "findElementsByName": (
            "Find cached mapped elements by case-insensitive display-name substring only. Use searchModelElements "
            "for IDs, metadata, types, roles, namespaces, profiles, or relationships. Returns paginated "
            "[i3X ElementId, display name] pairs; no live OPC UA calls."
        ),
        "findElementsByNamespace": (
            "List cached mapped elements belonging to an exact namespace URI as paginated "
            "[i3X ElementId, display name] pairs. Namespace membership does not prove companion-specification "
            "conformance. No live OPC UA calls."
        ),
        "findElementsByRole": (
            "List cached mapped elements of one exact i3X semantic role (asset, group, component, datapoint, "
            "property, or unknown) as paginated [i3X ElementId, display name] pairs. This is not an OPC UA "
            "NodeClass filter. No live OPC UA calls."
        ),
        "resolveElementIdsToNodeIds": (
            "Translate exact discovered i3X ElementIds to their source OPC UA NodeIds and, when available, "
            "ExpandedNodeIds. This maps cached identities; it does not verify live node existence or read values."
        ),
        "resolveNodeIdsToElementIds": (
            "Translate standard or expanded OPC UA NodeIds to matching cached i3X ElementIds. A missing match "
            "means the node is not in this mapped model, not necessarily absent from the remote address space."
        ),
        "searchModelElements": (
            "Search only the current cached i3X-mapped model by name, ID, type, semantic role, namespace, profile, "
            "or relationship. Use this for metadata/type/role searches; use findElementsByName for display-name-only "
            "searches. Supply a focused text or exact filter; omitting text may return an arbitrary first page. "
            "For OPC UA server identity, search for ServerStatus/Server and verify the candidate's standard "
            "ServerType (ns=0;i=2004) through its context/source metadata. Use getElementContext for one candidate "
            "and resolveElementIdsToNodeIds for the actual source NodeId. Results do not prove complete remote "
            "address-space visibility."
        ),
        "getElementContext": (
            "Get one cached model element with bounded hierarchy ancestors, direct children, semantic metadata, "
            "source NodeId/type metadata, and direct relationships. This describes the cached mapping; it does not "
            "browse or verify the live remote OPC UA address space."
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
                    "resolveBrowsePath",
                    "getBrowsePath",
                    "getInstancesOfType",
                }
                else "normal"
            ),
        )
    return tools
