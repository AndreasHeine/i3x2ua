"""MCP protocol and tools testing."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from fastapi.exceptions import HTTPException
from fastapi.testclient import TestClient

from i3x_server.mcp import _safe_internal_request_url, get_api_prefix
from tests.conftest import fastapi_app


def _resolve_openapi_schema(schema: Any, components: Mapping[str, Any]) -> Any:
    if not isinstance(schema, Mapping):
        return schema

    if "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str):
            return dict(schema)
        prefix = "#/components/schemas/"
        if not ref.startswith(prefix):
            return {"$ref": ref}
        schema_name = ref.removeprefix(prefix)
        schemas = components.get("schemas", {})
        if isinstance(schemas, Mapping) and schema_name in schemas:
            return _resolve_openapi_schema(schemas[schema_name], components)
        return {"$ref": ref}

    resolved: dict[str, Any] = dict(schema)
    for key in ("allOf", "anyOf", "oneOf"):
        value = resolved.get(key)
        if isinstance(value, list):
            resolved[key] = [_resolve_openapi_schema(item, components) for item in value]

    properties = resolved.get("properties")
    if isinstance(properties, Mapping):
        resolved["properties"] = {
            str(name): _resolve_openapi_schema(value, components) for name, value in properties.items()
        }

    items = resolved.get("items")
    if items is not None:
        resolved["items"] = _resolve_openapi_schema(items, components)

    return resolved


def _pick_concrete_schema(schema: Any) -> Any:
    if not isinstance(schema, Mapping):
        return schema

    for key in ("anyOf", "oneOf"):
        variants = schema.get(key)
        if isinstance(variants, list) and variants:
            for variant in variants:
                if isinstance(variant, Mapping) and variant.get("type") == "null":
                    continue
                return _pick_concrete_schema(variant)
            return _pick_concrete_schema(variants[0])

    return schema


def _sample_from_schema(schema: Any, *, property_name: str = "") -> Any:
    concrete = _pick_concrete_schema(schema)
    if not isinstance(concrete, Mapping):
        return "sample"

    enum_values = concrete.get("enum")
    if isinstance(enum_values, list) and enum_values:
        return enum_values[0]

    schema_type = concrete.get("type")
    if isinstance(schema_type, list):
        non_null_types = [item for item in schema_type if item != "null"]
        schema_type = non_null_types[0] if non_null_types else schema_type[0]

    if schema_type == "string":
        lowered = property_name.lower()
        if "time" in lowered:
            return "2026-01-01T00:00:00Z"
        if lowered.endswith("id") or lowered.endswith("ids"):
            return "property-abc"
        return "sample"

    if schema_type == "integer":
        minimum = concrete.get("minimum")
        if isinstance(minimum, int):
            return minimum
        return 1

    if schema_type == "number":
        minimum = concrete.get("minimum")
        if isinstance(minimum, (int, float)):
            return float(minimum)
        return 1.0

    if schema_type == "boolean":
        return False

    if schema_type == "array":
        item_schema = concrete.get("items", {"type": "string"})
        return [_sample_from_schema(item_schema, property_name=property_name.removesuffix("s"))]

    properties = concrete.get("properties")
    if schema_type == "object" or isinstance(properties, Mapping):
        if not isinstance(properties, Mapping):
            return {}
        required = concrete.get("required", [])
        if not isinstance(required, list):
            required = []
        result: dict[str, Any] = {}
        for key in required:
            if not isinstance(key, str):
                continue
            result[key] = _sample_from_schema(properties.get(key, {}), property_name=key)
        return result

    return {}


def _build_required_mcp_arguments(input_schema: Mapping[str, Any]) -> dict[str, Any]:
    properties = input_schema.get("properties", {})
    required = input_schema.get("required", [])
    if not isinstance(properties, Mapping) or not isinstance(required, list):
        return {}

    args: dict[str, Any] = {}
    for name in required:
        if not isinstance(name, str):
            continue
        args[name] = _sample_from_schema(properties.get(name, {}), property_name=name)
    return args


def _with_runtime_argument_overrides(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    subscription_id: str | None = None,
) -> dict[str, Any]:
    result = dict(arguments)

    if "elementId" in result:
        result["elementId"] = "property-abc"
    if "element_id" in result:
        result["element_id"] = "property-abc"

    body = result.get("body")
    if isinstance(body, Mapping):
        body_dict = dict(body)
        if "elementIds" in body_dict:
            body_dict["elementIds"] = ["property-abc"]
        if "startTime" in body_dict:
            body_dict.setdefault("startTime", "2026-01-01T00:00:00Z")
        if "endTime" in body_dict:
            body_dict.setdefault("endTime", "2026-01-02T00:00:00Z")
        if "maxDepth" in body_dict:
            body_dict.setdefault("maxDepth", 1)
        if tool_name == "updateObjectValue":
            body_dict = {"value": 123}
        if tool_name == "createSubscription":
            body_dict.setdefault("clientId", "mcp-runtime-smoke")
            body_dict.setdefault("displayName", "MCP Runtime Smoke")
        if subscription_id is not None:
            if "subscriptionId" in body_dict:
                body_dict["subscriptionId"] = subscription_id
            if "subscriptionIds" in body_dict:
                body_dict["subscriptionIds"] = [subscription_id]
        result["body"] = body_dict

    return result


def _operation_id_for(client: TestClient, method: str, path: str) -> str:
    openapi = client.get("/openapi.json").json()
    paths = openapi.get("paths", {})
    assert isinstance(paths, Mapping)
    methods = paths.get(path, {})
    assert isinstance(methods, Mapping), f"Path not found in OpenAPI: {path}"
    details = methods.get(method.lower(), {})
    assert isinstance(details, Mapping), f"Method not found in OpenAPI: {method} {path}"
    operation_id = details.get("operationId")
    assert isinstance(operation_id, str) and operation_id
    return operation_id


def test_openapi_json_is_source_of_truth(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200

    generated = response.json()
    assert isinstance(generated, dict)
    assert isinstance(generated.get("paths"), dict)

    assert isinstance(generated.get("paths"), dict)


def test_mcp_tool_input_schemas_match_openapi_contract(client: TestClient) -> None:
    openapi = client.get("/openapi.json").json()
    tools_payload = client.get("/mcp/tools").json()
    tools = tools_payload["tools"]

    components = openapi.get("components", {})
    assert isinstance(components, Mapping)

    paths = openapi.get("paths", {})
    assert isinstance(paths, Mapping)

    for path, methods in paths.items():
        if not isinstance(path, str) or not isinstance(methods, Mapping):
            continue
        if path.startswith("/mcp"):
            continue

        for method, details in methods.items():
            if not isinstance(method, str) or not isinstance(details, Mapping):
                continue
            operation_id = details.get("operationId")
            if not isinstance(operation_id, str) or path.endswith("/subscriptions/stream"):
                continue

            assert operation_id in tools, f"Missing MCP tool for operationId={operation_id}"
            tool = tools[operation_id]

            assert tool["method"] == method.upper()
            assert tool["path"] == path

            input_schema = tool.get("inputSchema", {})
            assert input_schema.get("type") == "object"
            assert input_schema.get("additionalProperties") is False

            properties = input_schema.get("properties", {})
            required = set(input_schema.get("required", []))
            assert isinstance(properties, Mapping)

            expected_required: set[str] = set()
            expected_property_names: set[str] = set()

            for parameter in details.get("parameters", []):
                if not isinstance(parameter, Mapping):
                    continue
                parameter_name = parameter.get("name")
                if not isinstance(parameter_name, str):
                    continue
                expected_property_names.add(parameter_name)
                expected_schema = _resolve_openapi_schema(parameter.get("schema", {"type": "string"}), components)
                assert properties.get(parameter_name) == expected_schema
                if parameter.get("required"):
                    expected_required.add(parameter_name)

            request_body = details.get("requestBody")
            if isinstance(request_body, Mapping):
                content = request_body.get("content", {})
                if isinstance(content, Mapping):
                    app_json = content.get("application/json")
                    if isinstance(app_json, Mapping) and "schema" in app_json:
                        expected_property_names.add("body")
                        expected_body_schema = _resolve_openapi_schema(app_json["schema"], components)
                        assert properties.get("body") == expected_body_schema
                        if request_body.get("required"):
                            expected_required.add("body")

            assert set(properties.keys()) == expected_property_names
            assert required == expected_required


def test_mcp_non_subscription_tools_runtime_smoke(client: TestClient) -> None:
    tools_response = client.get("/mcp/tools")
    assert tools_response.status_code == 200
    tools = tools_response.json()["tools"]

    skipped_tools = {
        _operation_id_for(client, "POST", "/v1/subscriptions"),
        _operation_id_for(client, "POST", "/v1/subscriptions/register"),
        _operation_id_for(client, "POST", "/v1/subscriptions/unregister"),
        _operation_id_for(client, "POST", "/v1/subscriptions/sync"),
        _operation_id_for(client, "POST", "/v1/subscriptions/delete"),
        _operation_id_for(client, "POST", "/v1/subscriptions/list"),
    }

    for tool_name, tool in tools.items():
        if tool_name in skipped_tools:
            continue

        input_schema = tool.get("inputSchema", {})
        assert isinstance(input_schema, Mapping)
        arguments = _build_required_mcp_arguments(input_schema)
        arguments = _with_runtime_argument_overrides(tool_name, arguments, subscription_id=None)

        response = client.post("/mcp/call", json={"tool": tool_name, "arguments": arguments})
        assert response.status_code in {200, 206, 404, 501}, (
            f"Unexpected status for {tool_name} with args {arguments}: {response.status_code} {response.text}"
        )

        if response.status_code == 400:
            payload = response.json()
            message = payload.get("error", {}).get("message", "")
            assert "Missing required arguments" not in message
            assert "Unexpected arguments" not in message
            assert "Missing required body" not in message


def test_mcp_subscription_tools_runtime_lifecycle(client: TestClient) -> None:
    create_subscription_tool = _operation_id_for(client, "POST", "/v1/subscriptions")
    register_tool = _operation_id_for(client, "POST", "/v1/subscriptions/register")
    list_tool = _operation_id_for(client, "POST", "/v1/subscriptions/list")
    sync_tool = _operation_id_for(client, "POST", "/v1/subscriptions/sync")
    unregister_tool = _operation_id_for(client, "POST", "/v1/subscriptions/unregister")
    delete_tool = _operation_id_for(client, "POST", "/v1/subscriptions/delete")

    create_response = client.post(
        "/mcp/call",
        json={
            "tool": create_subscription_tool,
            "arguments": {"body": {"clientId": "mcp-runtime-smoke", "displayName": "MCP Runtime Smoke"}},
        },
    )
    assert create_response.status_code == 200
    create_payload = create_response.json()
    subscription_id = create_payload["result"]["subscriptionId"]

    register_response = client.post(
        "/mcp/call",
        json={
            "tool": register_tool,
            "arguments": {
                "body": {
                    "clientId": "mcp-runtime-smoke",
                    "subscriptionId": subscription_id,
                    "elementIds": ["property-abc"],
                    "maxDepth": 1,
                }
            },
        },
    )
    assert register_response.status_code == 200

    list_response = client.post(
        "/mcp/call",
        json={
            "tool": list_tool,
            "arguments": {"body": {"clientId": "mcp-runtime-smoke", "subscriptionIds": [subscription_id]}},
        },
    )
    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert list_payload["results"][0]["result"]["subscriptionId"] == subscription_id

    sync_response = client.post(
        "/mcp/call",
        json={
            "tool": sync_tool,
            "arguments": {
                "body": {
                    "clientId": "mcp-runtime-smoke",
                    "subscriptionId": subscription_id,
                    "lastSequenceNumber": 0,
                }
            },
        },
    )
    assert sync_response.status_code == 200

    remove_response = client.post(
        "/mcp/call",
        json={
            "tool": unregister_tool,
            "arguments": {
                "body": {
                    "clientId": "mcp-runtime-smoke",
                    "subscriptionId": subscription_id,
                    "elementIds": ["property-abc"],
                    "maxDepth": 1,
                }
            },
        },
    )
    assert remove_response.status_code == 200

    delete_response = client.post(
        "/mcp/call",
        json={
            "tool": delete_tool,
            "arguments": {"body": {"clientId": "mcp-runtime-smoke", "subscriptionIds": [subscription_id]}},
        },
    )
    assert delete_response.status_code == 200
    delete_payload = delete_response.json()
    assert delete_payload["results"][0]["success"] is True


def test_mcp_tools_are_generated_from_openapi(client_without_tool_overrides: TestClient) -> None:
    response = client_without_tool_overrides.get("/mcp/tools")
    assert response.status_code == 200

    payload = response.json()
    tools = payload["tools"]
    namespaces_id = _operation_id_for(client_without_tool_overrides, "GET", "/v1/namespaces")
    query_values_id = _operation_id_for(client_without_tool_overrides, "POST", "/v1/objects/value")
    stream_id = _operation_id_for(client_without_tool_overrides, "POST", "/v1/subscriptions/stream")
    assert namespaces_id in tools
    assert query_values_id in tools
    assert stream_id not in tools

    namespaces_tool = tools[namespaces_id]
    assert isinstance(namespaces_tool.get("description"), str)
    assert namespaces_tool["description"]
    assert namespaces_tool.get("priority") == "normal"
    assert namespaces_tool.get("keywords") == []

    value_tool = tools[query_values_id]
    assert value_tool["method"] == "POST"
    assert value_tool["path"] == "/v1/objects/value"
    assert value_tool["input_schema"]["properties"]["body"]["properties"]["elementIds"]["type"] == "array"
    assert value_tool["inputSchema"]["properties"]["body"]["properties"]["elementIds"]["type"] == "array"


def test_mcp_support_is_disabled_by_default(client_without_mcp: TestClient) -> None:
    response = client_without_mcp.get("/mcp")
    assert response.status_code == 404

    response = client_without_mcp.get("/mcp/tools")
    assert response.status_code == 404

    openapi = client_without_mcp.get("/openapi.json").json()
    assert not any(path.startswith("/mcp") for path in openapi["paths"])


def test_mcp_endpoint_exposes_sse_discovery(client: TestClient) -> None:
    response = client.get("/mcp")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: endpoint" in response.text
    assert "/mcp" in response.text
    assert '"method": "notifications/prompts/list_changed"' in response.text
    assert '"method": "notifications/resources/list_changed"' in response.text
    assert '"method": "notifications/roots/list_changed"' in response.text


def test_mcp_initialize_request(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 1
    assert payload["result"]["protocolVersion"] == "2025-06-18"
    assert payload["result"]["capabilities"]["tools"]["listChanged"] is False
    assert payload["result"]["capabilities"]["prompts"]["listChanged"] is True
    assert payload["result"]["capabilities"]["resources"]["listChanged"] is True
    assert payload["result"]["capabilities"]["roots"]["listChanged"] is True


def test_mcp_tools_list_request(client: TestClient) -> None:
    namespaces_id = _operation_id_for(client, "GET", "/v1/namespaces")
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert response.status_code == 200
    payload = response.json()
    tools = payload["result"]["tools"]
    assert any(tool["name"] == namespaces_id for tool in tools)


def test_mcp_prompts_list_rest(client: TestClient) -> None:
    response = client.get("/mcp/prompts")
    assert response.status_code == 200
    payload = response.json()
    prompts = payload["prompts"]
    assert any(item["name"] == "machine_health_snapshot" for item in prompts)
    assert any(item["name"] == "alarm_triage" for item in prompts)


def test_mcp_prompts_get_rest(client: TestClient) -> None:
    response = client.get("/mcp/prompts/machine_health_snapshot")
    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "machine_health_snapshot"
    assert payload["inputs"] == ["asset_id", "lookback_minutes"]
    assert "{{asset_id}}" in payload["template"]


def test_mcp_prompts_execute_rest(client: TestClient) -> None:
    response = client.post(
        "/mcp/prompts/execute",
        json={
            "name": "machine_health_snapshot",
            "parameters": {"asset_id": "Press-01", "lookback_minutes": "60"},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "machine_health_snapshot"
    assert "Press-01" in payload["rendered"]


def test_mcp_prompts_list_request(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 21, "method": "prompts/list"},
    )
    assert response.status_code == 200
    payload = response.json()
    prompts = payload["result"]["prompts"]
    assert any(prompt["name"] == "machine_health_snapshot" for prompt in prompts)


def test_mcp_resources_list_rest(client: TestClient) -> None:
    response = client.get("/mcp/resources")
    assert response.status_code == 200
    payload = response.json()
    resources = payload["resources"]
    assert any(item["uri"] == "i3x://openapi" for item in resources)
    assert any(item["uri"] == "i3x://mcp-overrides" for item in resources)


def test_mcp_resource_read_rest(client: TestClient) -> None:
    response = client.post("/mcp/resources/read", json={"uri": "i3x://openapi"})
    assert response.status_code == 200
    payload = response.json()
    contents = payload["contents"]
    assert len(contents) == 1
    assert contents[0]["uri"] == "i3x://openapi"
    assert contents[0]["mimeType"] == "application/json"


def test_mcp_roots_list_rest(client: TestClient) -> None:
    response = client.get("/mcp/roots")
    assert response.status_code == 200
    payload = response.json()
    roots = payload["roots"]
    assert any(item["uri"] == "i3x://roots/asset-root" for item in roots)


def test_mcp_resources_list_request(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 24, "method": "resources/list"},
    )
    assert response.status_code == 200
    payload = response.json()
    resources = payload["result"]["resources"]
    assert any(item["uri"] == "i3x://docs/quick-reference" for item in resources)


def test_mcp_resources_read_request(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 25,
            "method": "resources/read",
            "params": {"uri": "i3x://mcp-overrides"},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    contents = payload["result"]["contents"]
    assert len(contents) == 1
    assert contents[0]["uri"] == "i3x://mcp-overrides"


def test_mcp_roots_list_request(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 26, "method": "roots/list"},
    )
    assert response.status_code == 200
    payload = response.json()
    roots = payload["result"]["roots"]
    assert any(item["uri"] == "i3x://roots/asset-root" for item in roots)


def test_mcp_batch_request_returns_multiple_results(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json=[
            {"jsonrpc": "2.0", "id": 27, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 28, "method": "prompts/list"},
        ],
    )
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    assert len(payload) == 2
    ids = {item["id"] for item in payload}
    assert ids == {27, 28}


def test_mcp_empty_batch_returns_invalid_request(client: TestClient) -> None:
    response = client.post("/mcp", json=[])
    assert response.status_code == 200
    payload = response.json()
    assert payload["error"]["code"] == -32600


def test_mcp_invalid_jsonrpc_version_returns_invalid_request(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "1.0", "id": 29, "method": "tools/list"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == 29
    assert payload["error"]["code"] == -32600


def test_mcp_prompts_get_request(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 22,
            "method": "prompts/get",
            "params": {"name": "machine_health_snapshot"},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    prompt = payload["result"]
    assert prompt["name"] == "machine_health_snapshot"
    assert prompt["inputs"] == ["asset_id", "lookback_minutes"]


def test_mcp_prompts_execute_request(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 23,
            "method": "prompts/execute",
            "params": {
                "name": "machine_health_snapshot",
                "parameters": {"asset_id": "Press-01", "lookback_minutes": "60"},
            },
        },
    )
    assert response.status_code == 200
    payload = response.json()
    result = payload["result"]
    assert result["name"] == "machine_health_snapshot"
    assert "Press-01" in result["rendered"]


def test_mcp_tools_call_request(client: TestClient) -> None:
    namespaces_id = _operation_id_for(client, "GET", "/v1/namespaces")
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": namespaces_id, "arguments": {}},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    content = payload["result"]["content"]
    assert content[0]["type"] == "text"
    assert "success" in content[0]["text"]


def test_mcp_initialize_notification_returns_no_response(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
    )

    assert response.status_code == 202
    assert response.text == "null"


def test_mcp_tools_list_notification_returns_no_response(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "method": "tools/list",
        },
    )

    assert response.status_code == 202
    assert response.text == "null"


def test_mcp_tools_call_notification_returns_no_response(client: TestClient) -> None:
    namespaces_id = _operation_id_for(client, "GET", "/v1/namespaces")
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": namespaces_id, "arguments": {}},
        },
    )

    assert response.status_code == 202
    assert response.text == "null"


def test_mcp_call_allows_omitting_optional_query_parameters(client: TestClient) -> None:
    tools_response = client.get("/mcp/tools")
    assert tools_response.status_code == 200
    tools = tools_response.json()["tools"]

    candidate_name: str | None = None
    for name, tool in tools.items():
        query_parameters = set(tool.get("query_parameters", []))
        required_fields = set(tool.get("input_schema", {}).get("required", []))
        required_query_parameters = query_parameters & required_fields
        if (
            query_parameters
            and not required_query_parameters
            and not tool.get("path_parameters")
            and not tool.get("body_required", False)
            and tool.get("method") == "GET"
        ):
            candidate_name = name
            break

    if candidate_name is None:
        pytest.skip("No MCP tool with fully optional query parameters is available")

    response = client.post("/mcp/call", json={"tool": candidate_name, "arguments": {}})
    assert response.status_code == 200


def test_mcp_jsonrpc_tools_call_returns_jsonrpc_error_for_http_exception(client: TestClient) -> None:
    update_value_id = _operation_id_for(client, "PUT", "/v1/objects/{element_id}/value")
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 301,
            "method": "tools/call",
            "params": {"name": update_value_id, "arguments": {}},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 301
    assert payload["error"]["code"] == 400
    assert "Missing required arguments" in payload["error"]["message"]


def test_mcp_call_dispatches_to_existing_api(client: TestClient) -> None:
    namespaces_id = _operation_id_for(client, "GET", "/v1/namespaces")
    response = client.post("/mcp/call", json={"tool": namespaces_id, "arguments": {}})
    assert response.status_code == 200

    expected = client.get("/v1/namespaces")
    assert response.json() == expected.json()


def test_mcp_call_supports_body_arguments(client: TestClient) -> None:
    query_values_id = _operation_id_for(client, "POST", "/v1/objects/value")
    response = client.post(
        "/mcp/call",
        json={
            "tool": query_values_id,
            "arguments": {
                "body": {
                    "elementIds": ["property-abc"],
                    "maxDepth": 1,
                },
            },
        },
    )
    assert response.status_code == 200

    payload = response.json()
    assert payload["success"] is True
    assert payload["results"][0]["success"] is True
    assert payload["results"][0]["result"]["isComposition"] is False


@pytest.mark.parametrize("element_id", ["http://evil.example", "../evil"])
def test_mcp_call_rejects_malicious_path_parameters(client: TestClient, element_id: str) -> None:
    update_value_id = _operation_id_for(client, "PUT", "/v1/objects/{element_id}/value")
    response = client.post(
        "/mcp/call",
        json={"tool": update_value_id, "arguments": {"element_id": element_id}},
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["message"] == "Invalid path parameter: element_id"


def test_mcp_call_rejects_unknown_tool(client: TestClient) -> None:
    response = client.post("/mcp/call", json={"tool": "unknownTool", "arguments": {}})
    assert response.status_code == 400


def test_mcp_get_api_prefix_strips_host_parts() -> None:
    openapi_spec = {"servers": [{"url": "https://example.test/v1"}]}
    assert get_api_prefix(openapi_spec) == "/v1"


@pytest.mark.parametrize("path", ["http://evil.example/pwn", "//evil.example/pwn"])
def test_mcp_internal_request_url_rejects_external_hosts(path: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        _safe_internal_request_url(path)
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["error"]["message"] in {"Invalid MCP request path", "Invalid MCP tool path"}


def test_mcp_internal_request_url_keeps_fixed_internal_host() -> None:
    url = _safe_internal_request_url("/v1/namespaces")
    assert url.host == "mcp.local"
    assert str(url) == "http://mcp.local/v1/namespaces"


def test_mcp_call_strips_host_from_runtime_api_prefix(client: TestClient) -> None:
    app = fastapi_app(client)
    app.state.mcp_api_prefix = "https://evil.example"

    namespaces_id = _operation_id_for(client, "GET", "/v1/namespaces")
    response = client.post("/mcp/call", json={"tool": namespaces_id, "arguments": {}})
    assert response.status_code == 200
