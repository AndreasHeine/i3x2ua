from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import nullcontext as _nullcontext
from dataclasses import dataclass
from posixpath import normpath
from time import perf_counter
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from i3x_server.errors import i3x_http_error
from i3x_server.mcp.model_access import MCP_MODEL_ACCESS_SCHEMAS, invoke_model_access_tool
from i3x_server.mcp.model_tools import (
    MCP_MODEL_TOOL_SCHEMAS,
    _get_element_context,
    _resolve_element_ids_to_node_ids,
    _resolve_node_ids_to_element_ids,
    _search_model_elements,
    _summarize_subtree,
    _trace_relationship_path,
    _validate_model_tool_arguments,
)
from i3x_server.mcp.tool_definitions import (
    DEFAULT_MCP_OVERRIDES_PATH,
    DEFAULT_MCP_OVERRIDES_SCHEMA_PATH,
    MCP_BODY_PAGINATED_TOOLS,
    MCP_CONTAINER_VALUES_BATCH_SIZE,
    MCP_CONTAINER_VALUES_MAX_DEPTH,
    MCP_CONTAINER_VALUES_MAX_NODES,
    MCP_CONTAINER_VALUES_TOOL,
    MCP_DEFAULT_PAGE_SIZE,
    MCP_PAGINATED_LIST_TOOLS,
    McpToolDefinition,
    _resolve_override_path,
    _resolve_request_body_schema,
    _resolve_schema,
    build_mcp_tools,
    load_feature_overrides,
    load_overrides,
    load_prompt_overrides,
    load_tool_overrides,
)
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

McpModelProvider = Callable[[Request], Awaitable[BuildResult]]
_TRACEPARENT_PATTERN = re.compile(
    r"^[\da-f]{2}-([\da-f]{32})-([\da-f]{16})-[\da-f]{2}$",
    flags=re.IGNORECASE,
)
logger = logging.getLogger(__name__)

__all__ = [
    "McpToolDefinition",
    "_resolve_override_path",
    "_resolve_request_body_schema",
    "_resolve_schema",
    "DEFAULT_MCP_OVERRIDES_PATH",
    "DEFAULT_MCP_OVERRIDES_SCHEMA_PATH",
    "build_mcp_tools",
    "get_api_prefix",
    "load_feature_overrides",
    "load_overrides",
    "load_prompt_overrides",
    "load_tool_overrides",
]


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


@dataclass(frozen=True, slots=True)
class _InternalASGIResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


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


async def _invoke_cached_model_access_tool(
    request: Request,
    tool_name: str,
    arguments: Mapping[str, Any],
) -> _InternalASGIResponse:
    model = getattr(request.app.state, "model_cache", None)
    if not isinstance(model, BuildResult):
        raise i3x_http_error(503, "Service Unavailable", "Cached model is not available")
    context = None
    if tool_name == "getInstancesOfType":
        from i3x_server.api.v1.objecttype_helpers import _ObjectTypeContext

        cache = getattr(request.app.state, "object_type_context_cache", None)
        if isinstance(cache, dict) and cache.get("model_token") == model_revision(model):
            candidate = cache.get("context")
            if isinstance(candidate, _ObjectTypeContext):
                context = candidate
    body = invoke_model_access_tool(model, tool_name, arguments, context)
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
            elif tool.name in MCP_MODEL_ACCESS_SCHEMAS:
                response = await _invoke_cached_model_access_tool(request, tool.name, arguments)
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
