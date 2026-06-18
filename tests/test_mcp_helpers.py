from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from i3x_server.mcp import (
    McpToolDefinition,
    _invoke_internal_asgi,
    _payload_to_response,
    _resolve_override_path,
    _resolve_request_body_schema,
    _resolve_schema,
    _safe_api_prefix,
    _safe_internal_request_url,
    _safe_path_parameter_value,
    _safe_request_path,
    _trace_log_fields,
    build_mcp_tools,
    get_api_prefix,
    invoke_mcp_tool,
    load_feature_overrides,
    load_overrides,
    load_prompt_overrides,
    load_tool_overrides,
)


def _request() -> Request:
    app = FastAPI()
    app.state.mcp_api_prefix = ""
    return Request({"type": "http", "app": app, "headers": []})


def test_safe_prefix_and_path_helpers() -> None:
    assert _safe_api_prefix("/api/v1") == "/api/v1"
    assert _safe_api_prefix("http://localhost:8000/api") == "/api"
    assert _safe_api_prefix("/") == ""

    assert _safe_api_prefix(" //evil") == ""

    assert _safe_request_path("/api", "/v1/objects") == "/api/v1/objects"
    with pytest.raises(HTTPException):
        _safe_request_path("/api", "v1/objects")
    with pytest.raises(HTTPException):
        _safe_request_path("/api", "/v1/objects?x=1")

    assert _safe_path_parameter_value("id", "abc") == "abc"
    with pytest.raises(HTTPException):
        _safe_path_parameter_value("id", "../bad")

    url = _safe_internal_request_url("/v1/objects")
    assert str(url).startswith("http://mcp.local/")


def test_resolve_schema_and_request_body_schema() -> None:
    components = {
        "schemas": {
            "Item": {"type": "object", "properties": {"id": {"type": "string"}}},
            "Bag": {"type": "array", "items": {"$ref": "#/components/schemas/Item"}},
        }
    }
    resolved_item = _resolve_schema({"$ref": "#/components/schemas/Item"}, components)
    assert resolved_item["properties"]["id"]["type"] == "string"

    resolved_bag = _resolve_schema({"$ref": "#/components/schemas/Bag"}, components)
    assert resolved_bag["items"]["properties"]["id"]["type"] == "string"

    schema = _resolve_request_body_schema(
        {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Item"}}}},
        components,
    )
    assert isinstance(schema, dict)
    assert schema["properties"]["id"]["type"] == "string"

    assert _resolve_request_body_schema({}, components) is None


def test_build_mcp_tools_and_api_prefix_from_openapi() -> None:
    openapi = {
        "servers": [{"url": "/api"}],
        "components": {"schemas": {"Payload": {"type": "object", "properties": {"x": {"type": "integer"}}}}},
        "paths": {
            "/v1/objects/{id}": {
                "get": {
                    "operationId": "getObject",
                    "summary": "Get object",
                    "parameters": [
                        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
                        {"name": "expand", "in": "query", "required": False, "schema": {"type": "boolean"}},
                    ],
                }
            },
            "/v1/update": {
                "post": {
                    "operationId": "updateObject",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Payload"}}},
                    },
                }
            },
            "/mcp/tools": {"get": {"operationId": "internalMcp"}},
        },
    }
    tools = build_mcp_tools(openapi)
    assert set(tools.keys()) == {"getObject", "updateObject"}
    assert tools["getObject"].path_parameters == ("id",)
    assert tools["updateObject"].body_required is True
    assert get_api_prefix(openapi) == "/api"


def test_load_overrides_variants(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "tools": {"type": "object"},
                    "prompts": {"type": "object"},
                    "features": {"type": "object"},
                },
                "additionalProperties": True,
            }
        ),
        encoding="utf-8",
    )
    overrides_path = tmp_path / "mcp_overrides.json"
    overrides_path.write_text(
        json.dumps(
            {
                "tools": {"toolA": {"description": "A"}},
                "prompts": {"promptA": {"description": "P", "inputs": ["x"], "template": "{{x}}"}},
                "features": {"featureA": True},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_overrides(overrides_path, schema_path)
    assert "tools" in loaded
    assert load_tool_overrides(overrides_path, schema_path) == {"toolA": {"description": "A"}}
    assert "promptA" in load_prompt_overrides(overrides_path, schema_path)
    assert load_feature_overrides(overrides_path, schema_path) == {"featureA": True}

    missing = tmp_path / "missing.json"
    assert load_overrides(missing, schema_path) == {}

    invalid = tmp_path / "invalid.json"
    invalid.write_text("[1,2,3]", encoding="utf-8")
    assert load_overrides(invalid, schema_path) == {}

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    assert load_overrides(malformed, schema_path) == {}


def test_resolve_override_path_absolute_and_relative(tmp_path: Path) -> None:
    absolute = tmp_path / "overrides.json"
    assert _resolve_override_path(absolute) == absolute
    resolved = _resolve_override_path("overrides/mcp_overrides.json")
    assert resolved.name == "mcp_overrides.json"


@pytest.mark.asyncio
async def test_invoke_mcp_tool_validation_errors() -> None:
    request = _request()
    tool = McpToolDefinition(
        name="getObject",
        description="Get object",
        method="GET",
        path="/v1/objects/{id}",
        input_schema={
            "type": "object",
            "properties": {"id": {"type": "string"}, "body": {"type": "object"}},
            "required": ["id"],
        },
        path_parameters=("id",),
        query_parameters=(),
        body_required=False,
    )

    with pytest.raises(HTTPException) as unexpected:
        await invoke_mcp_tool(request, tool, {"id": "a", "extra": 1})
    assert unexpected.value.status_code == 400

    with pytest.raises(HTTPException) as missing_required:
        await invoke_mcp_tool(request, tool, {})
    assert missing_required.value.status_code == 400

    body_tool = McpToolDefinition(
        name="postObject",
        description="Post object",
        method="POST",
        path="/v1/update",
        input_schema={"type": "object", "properties": {"body": {"type": "object"}}, "required": ["body"]},
        path_parameters=(),
        query_parameters=(),
        body_required=True,
    )
    with pytest.raises(HTTPException) as missing_body:
        await invoke_mcp_tool(request, body_tool, {})
    assert missing_body.value.status_code == 400


def test_payload_to_response_variants() -> None:
    json_response = _payload_to_response({"status_code": 200, "body": {"ok": True}})
    assert json_response.status_code == 200

    text_response = _payload_to_response(
        {
            "status_code": 202,
            "body": {"text": "accepted", "content_type": "text/plain"},
        }
    )
    assert text_response.status_code == 202

    passthrough = _payload_to_response({"hello": "world"})
    assert passthrough.status_code == 200


def test_trace_log_fields_from_header_and_default(monkeypatch: pytest.MonkeyPatch) -> None:
    request_with_trace = _request()
    request_with_trace.scope["headers"] = [
        (b"traceparent", b"00-0123456789abcdef0123456789abcdef-0123456789abcdef-01")
    ]
    assert _trace_log_fields(request_with_trace) == ("0123456789abcdef0123456789abcdef", "0123456789abcdef")

    monkeypatch.setattr("i3x_server.mcp.get_current_span", None)
    request_without_trace = _request()
    request_without_trace.scope["headers"] = []
    assert _trace_log_fields(request_without_trace) == ("-", "-")


@pytest.mark.asyncio
async def test_invoke_internal_asgi_and_call_path_helpers() -> None:
    app = FastAPI()

    @app.get("/ok")
    async def ok() -> dict[str, bool]:
        return {"ok": True}

    response = await _invoke_internal_asgi(app=app, method="GET", path="/ok", query_params={}, request_body=None)
    assert response.status_code == 200
    assert response.headers.get("content-type", "").startswith("application/json")

    post_app = FastAPI()

    @post_app.post("/echo")
    async def echo(payload: dict[str, object]) -> dict[str, object]:
        return payload

    echoed = await _invoke_internal_asgi(
        app=post_app,
        method="POST",
        path="/echo",
        query_params={},
        request_body={"x": 1},
    )
    assert echoed.status_code == 200
    assert json.loads(echoed.body.decode("utf-8")) == {"x": 1}
