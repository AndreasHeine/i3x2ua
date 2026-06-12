from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from i3x_server.errors import i3x_http_error

MCP_EXCLUDED_OPERATION_IDS = {"streamSubscription"}


def load_overrides(path: str | Path = "tool_overrides.json") -> dict[str, Any]:
    override_path = Path(path)
    if not override_path.is_absolute():
        override_path = Path(__file__).resolve().parents[1] / override_path
    if not override_path.exists():
        return {}

    with override_path.open("r", encoding="utf-8") as file:
        overrides = json.load(file)

    return overrides if isinstance(overrides, dict) else {}


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_mcp_tools(
    openapi_spec: Mapping[str, Any],
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, McpToolDefinition]:
    if overrides is None:
        overrides = load_overrides()

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
        for method, details in methods.items():
            if not isinstance(method, str) or not isinstance(details, Mapping):
                continue
            operation_id = details.get("operationId")
            if not isinstance(operation_id, str) or operation_id in MCP_EXCLUDED_OPERATION_IDS:
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
                    input_properties["body"] = body_schema
                    if body_required:
                        required.append("body")

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
            )

    return tools


def get_api_prefix(openapi_spec: Mapping[str, Any]) -> str:
    servers = openapi_spec.get("servers")
    if isinstance(servers, list):
        for server in servers:
            if not isinstance(server, Mapping):
                continue
            url = server.get("url")
            if isinstance(url, str) and url:
                return url.rstrip("/")
    return ""


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


async def call_mcp_tool(request: Request, tool: McpToolDefinition, arguments: Mapping[str, Any]) -> Response:
    payload = await invoke_mcp_tool(request, tool, arguments)
    return _payload_to_response(payload)


async def invoke_mcp_tool(request: Request, tool: McpToolDefinition, arguments: Mapping[str, Any]) -> Any:
    allowed_arguments = set(tool.path_parameters) | set(tool.query_parameters)
    if tool.body_required or "body" in tool.input_schema.get("properties", {}):
        allowed_arguments.add("body")

    unexpected_arguments = sorted(set(arguments) - allowed_arguments)
    if unexpected_arguments:
        raise i3x_http_error(400, "Bad Request", f"Unexpected arguments: {', '.join(unexpected_arguments)}")

    missing_path_parameters = [name for name in tool.path_parameters if name not in arguments]
    missing_query_parameters = [name for name in tool.query_parameters if name not in arguments]
    missing_arguments = missing_path_parameters + missing_query_parameters
    if missing_arguments:
        raise i3x_http_error(400, "Bad Request", f"Missing required arguments: {', '.join(missing_arguments)}")

    api_prefix = getattr(request.app.state, "mcp_api_prefix", "")
    if not isinstance(api_prefix, str):
        api_prefix = ""

    resolved_path = f"{api_prefix}{tool.path}" if api_prefix else tool.path
    for parameter_name in tool.path_parameters:
        placeholder = "{" + parameter_name + "}"
        if placeholder not in resolved_path:
            raise i3x_http_error(500, "Internal Error", f"Path placeholder not found for {parameter_name}")
        resolved_path = resolved_path.replace(placeholder, str(arguments[parameter_name]))

    query_params = {
        parameter_name: arguments[parameter_name]
        for parameter_name in tool.query_parameters
        if arguments.get(parameter_name) is not None
    }

    request_body = arguments.get("body") if "body" in arguments else None
    if tool.body_required and request_body is None:
        raise i3x_http_error(400, "Bad Request", "Missing required body")

    transport = httpx.ASGITransport(app=request.app)
    request_kwargs: dict[str, Any] = {"params": query_params}
    if request_body is not None:
        request_kwargs["json"] = request_body

    async with httpx.AsyncClient(transport=transport, base_url="http://mcp.local", timeout=30) as client:
        response = await client.request(tool.method, resolved_path, **request_kwargs)

    if response.headers.get("content-type", "").startswith("application/json"):
        try:
            body = response.json()
        except ValueError as exc:
            raise i3x_http_error(502, "Bad Gateway", "Upstream response was not valid JSON") from exc
    else:
        body = {
            "text": response.text,
            "content_type": response.headers.get("content-type"),
        }

    return {"status_code": response.status_code, "body": body}


def _payload_to_response(payload: Any) -> Response:
    if isinstance(payload, dict) and "status_code" in payload and "body" in payload:
        body = payload["body"]
        status_code = int(payload["status_code"])
        if isinstance(body, dict) and "text" in body and "content_type" in body:
            return Response(content=str(body["text"]), status_code=status_code, media_type=body.get("content_type"))
        return JSONResponse(status_code=status_code, content=body)
    return JSONResponse(status_code=200, content=payload)
