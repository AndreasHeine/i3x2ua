from __future__ import annotations

import json
from typing import Any, cast

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from jsonschema import validate

from i3x_server.api.mcp.monolithic import (
    MCP_IDENTITY_GUIDE_URI,
    MCP_META_CLIENT_CAPABILITIES,
    MCP_META_PROTOCOL_VERSION,
    MCP_PROTOCOL_VERSION,
    MCP_SESSION_HEADER,
    MCP_SUPPORTED_PROTOCOL_VERSIONS,
    _compact_tool_body,
    publish_mcp_notification,
    router,
    shutdown_mcp_transport,
)
from i3x_server.mcp import build_mcp_tools
from i3x_server.prompts.registry import PromptDefinition, PromptRegistry


@pytest.fixture
def protocol_client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.mcp_allowed_origins = ["https://trusted.example"]
    app.state.mcp_tools = build_mcp_tools(
        {
            "paths": {
                "/v1/objects/value": {
                    "post": {
                        "operationId": "readValues",
                        "requestBody": {
                            "required": True,
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        },
                    }
                },
                "/v1/subscriptions": {"post": {"operationId": "createSubscription"}},
                "/v1/subscriptions/sync": {"post": {"operationId": "syncSubscription"}},
            }
        },
        overrides={},
    )
    app.state.mcp_prompts = PromptRegistry(
        {"inspect": PromptDefinition("inspect", "Inspect asset", ("asset",), "Inspect {{asset}}")}
    )
    return TestClient(app)


def rpc(client: TestClient, method: str, params: Any = None) -> dict[str, Any]:
    payload = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).json()
    assert isinstance(payload, dict)
    return payload


def modern_rpc(
    client: TestClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    protocol_version: str = MCP_PROTOCOL_VERSION,
) -> Any:
    request_params = dict(params or {})
    request_params["_meta"] = {
        MCP_META_PROTOCOL_VERSION: protocol_version,
        MCP_META_CLIENT_CAPABILITIES: {},
    }
    headers = {
        "MCP-Protocol-Version": protocol_version,
        "Mcp-Method": method,
    }
    if method in {"tools/call", "prompts/get"} and isinstance(request_params.get("name"), str):
        headers["Mcp-Name"] = request_params["name"]
    elif method == "resources/read" and isinstance(request_params.get("uri"), str):
        headers["Mcp-Name"] = request_params["uri"]
    return client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": request_params},
    )


def test_version_negotiation_and_ping(protocol_client: TestClient) -> None:
    result = rpc(protocol_client, "initialize", {"protocolVersion": "2099-01-01"})["result"]
    assert result["protocolVersion"] == "2025-11-25"
    assert "roots" not in result["capabilities"]
    assert result["instructions"]
    assert rpc(protocol_client, "ping")["result"] == {}


@pytest.mark.parametrize("version", ["2025-03-26", "2025-06-18", "2025-11-25"])
def test_legacy_version_negotiation_preserves_supported_version(protocol_client: TestClient, version: str) -> None:
    result = rpc(protocol_client, "initialize", {"protocolVersion": version})["result"]
    assert result["protocolVersion"] == version


def test_legacy_session_enforces_negotiated_version(protocol_client: TestClient) -> None:
    initialized = protocol_client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        },
    )
    session_id = initialized.headers[MCP_SESSION_HEADER]
    request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
    }

    accepted = protocol_client.post(
        "/mcp",
        headers={MCP_SESSION_HEADER: session_id, "MCP-Protocol-Version": "2025-11-25"},
        json=request,
    )
    assert accepted.status_code == 200

    rejected = protocol_client.post(
        "/mcp",
        headers={MCP_SESSION_HEADER: session_id, "MCP-Protocol-Version": "2025-06-18"},
        json=request,
    )
    assert rejected.status_code == 400


def test_modern_discovery_and_requests_are_stateless(protocol_client: TestClient) -> None:
    discovery_response = modern_rpc(protocol_client, "server/discover")
    assert discovery_response.status_code == 200
    discovery = discovery_response.json()["result"]
    assert discovery["supportedVersions"] == list(MCP_SUPPORTED_PROTOCOL_VERSIONS)
    assert discovery["capabilities"] == {"tools": {}, "prompts": {}, "resources": {}}
    assert discovery["resultType"] == "complete"
    assert discovery["_meta"][MCP_META_PROTOCOL_VERSION] == MCP_PROTOCOL_VERSION
    assert "io.modelcontextprotocol/serverInfo" in discovery["_meta"]

    tools_response = modern_rpc(protocol_client, "tools/list")
    assert tools_response.status_code == 200
    tools_result = tools_response.json()["result"]
    assert tools_result["tools"]
    assert tools_result["resultType"] == "complete"
    assert tools_result["ttlMs"] == 0
    assert tools_result["cacheScope"] == "private"
    assert tools_result["_meta"][MCP_META_PROTOCOL_VERSION] == MCP_PROTOCOL_VERSION

    prompts_result = modern_rpc(protocol_client, "prompts/list").json()["result"]
    resources_result = modern_rpc(protocol_client, "resources/list").json()["result"]
    templates_result = modern_rpc(protocol_client, "resources/templates/list").json()["result"]
    for result in (prompts_result, resources_result, templates_result):
        assert result["resultType"] == "complete"
        assert result["ttlMs"] == 0
        assert result["cacheScope"] == "private"
    mapping_guide = next(
        resource for resource in resources_result["resources"] if resource["uri"] == "i3x://opcua-to-i3x-mapping"
    )
    assert mapping_guide["mimeType"] == "text/markdown"
    identity_guide = next(
        resource for resource in resources_result["resources"] if resource["uri"] == MCP_IDENTITY_GUIDE_URI
    )
    assert identity_guide["mimeType"] == "text/markdown"

    read_result = modern_rpc(
        protocol_client,
        "resources/read",
        {"uri": mapping_guide["uri"]},
    ).json()["result"]
    assert read_result["contents"][0]["mimeType"] == "text/markdown"
    assert "Composition" in read_result["contents"][0]["text"]
    assert read_result["ttlMs"] == 0
    assert read_result["cacheScope"] == "private"

    identity_result = modern_rpc(
        protocol_client,
        "resources/read",
        {"uri": identity_guide["uri"]},
    ).json()["result"]
    assert "typeElementId" in identity_result["contents"][0]["text"]
    assert "SHA-1" in identity_result["contents"][0]["text"]


def test_modern_protocol_validation_errors(protocol_client: TestClient) -> None:
    missing_metadata = protocol_client.post(
        "/mcp",
        headers={"MCP-Protocol-Version": MCP_PROTOCOL_VERSION, "Mcp-Method": "tools/list"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert missing_metadata.status_code == 400
    assert missing_metadata.json()["error"]["code"] == -32602

    mismatch = protocol_client.post(
        "/mcp",
        headers={
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            "Mcp-Method": "ping",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {
                "_meta": {
                    MCP_META_PROTOCOL_VERSION: MCP_PROTOCOL_VERSION,
                    MCP_META_CLIENT_CAPABILITIES: {},
                }
            },
        },
    )
    assert mismatch.status_code == 400
    assert mismatch.json()["error"]["code"] == -32020

    name_mismatch = protocol_client.post(
        "/mcp",
        headers={
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            "Mcp-Method": "tools/call",
            "Mcp-Name": "otherTool",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "readValues",
                "arguments": {"body": {}},
                "_meta": {
                    MCP_META_PROTOCOL_VERSION: MCP_PROTOCOL_VERSION,
                    MCP_META_CLIENT_CAPABILITIES: {},
                },
            },
        },
    )
    assert name_mismatch.status_code == 400
    assert name_mismatch.json()["error"]["code"] == -32020

    unsupported_version = modern_rpc(protocol_client, "tools/list", protocol_version="2099-01-01")
    assert unsupported_version.status_code == 400
    assert unsupported_version.json()["error"]["code"] == -32022
    assert unsupported_version.json()["error"]["data"] == {
        "supported": list(MCP_SUPPORTED_PROTOCOL_VERSIONS),
        "requested": "2099-01-01",
    }


def test_standard_prompt_and_resource_contracts(protocol_client: TestClient) -> None:
    prompt = rpc(protocol_client, "prompts/list")["result"]["prompts"][0]
    assert prompt["arguments"] == [{"name": "asset", "required": True}]
    result = rpc(protocol_client, "prompts/get", {"name": "inspect", "arguments": {"asset": "Pump"}})["result"]
    assert result["messages"] == [{"role": "user", "content": {"type": "text", "text": "Inspect Pump"}}]
    assert rpc(protocol_client, "prompts/get", {"name": "inspect"})["error"]["code"] == -32602
    invalid = rpc(protocol_client, "prompts/get", {"name": "inspect", "arguments": {"asset": 1}})
    assert invalid["error"]["code"] == -32602
    assert rpc(protocol_client, "resources/templates/list")["result"] == {"resourceTemplates": []}
    assert rpc(protocol_client, "roots/list")["error"]["code"] == -32601


def test_http_contracts(protocol_client: TestClient) -> None:
    notification = protocol_client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert notification.status_code == 202
    assert notification.content == b""
    assert protocol_client.get("/mcp").status_code == 406
    assert protocol_client.get("/mcp", headers={"Accept": "text/event-stream"}).status_code == 400
    assert protocol_client.post("/mcp", content="{").json()["error"]["code"] == -32700
    assert (
        protocol_client.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}]).json()["error"]["code"]
        == -32600
    )
    assert (
        protocol_client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"MCP-Protocol-Version": "2099-01-01"}
        ).status_code
        == 400
    )


def test_sse_session_notifications_and_cleanup(protocol_client: TestClient) -> None:
    initialized = protocol_client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
    )
    assert initialized.status_code == 200
    session_id = initialized.headers[MCP_SESSION_HEADER]
    app = cast(FastAPI, protocol_client.app)
    session = app.state.mcp_transport_sessions[session_id]

    with protocol_client.stream(
        "GET",
        "/mcp",
        headers={"Accept": "text/event-stream", MCP_SESSION_HEADER: session_id},
    ) as stream:
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert (
            publish_mcp_notification(
                app,
                session_id,
                "notifications/message",
                {"level": "info", "data": "connected"},
            )
            == 1
        )
        lines = iter(stream.iter_lines())
        assert next(lines) == "event: message"
        assert json.loads(next(lines).removeprefix("data: ")) == {
            "jsonrpc": "2.0",
            "method": "notifications/message",
            "params": {"level": "info", "data": "connected"},
        }
        assert next(lines) == ""
        deleted = protocol_client.delete("/mcp", headers={MCP_SESSION_HEADER: session_id})
        assert deleted.status_code == 200
        assert list(lines) == []

    assert session_id not in app.state.mcp_transport_sessions
    assert not session.streams
    assert publish_mcp_notification(app, session_id, "notifications/message") == 0


def test_sse_stream_closes_on_transport_shutdown(protocol_client: TestClient) -> None:
    initialized = protocol_client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
    )
    session_id = initialized.headers[MCP_SESSION_HEADER]
    app = cast(FastAPI, protocol_client.app)
    session = app.state.mcp_transport_sessions[session_id]

    with protocol_client.stream(
        "GET",
        "/mcp",
        headers={"Accept": "text/event-stream", MCP_SESSION_HEADER: session_id},
    ) as stream:
        assert stream.status_code == 200
        shutdown_mcp_transport(app)
        assert list(stream.iter_lines()) == []

    assert app.state.mcp_transport_sessions == {}
    assert not session.streams


@pytest.mark.parametrize("origin, expected", [("https://evil.example", 403), ("https://trusted.example", 200)])
def test_origin_validation(protocol_client: TestClient, origin: str, expected: int) -> None:
    assert (
        protocol_client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Origin": origin}
        ).status_code
        == expected
    )


@pytest.mark.parametrize(
    "message",
    [
        {"jsonrpc": "2.0", "id": None, "method": "ping"},
        {"jsonrpc": "2.0", "id": True, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1.5, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1},
    ],
)
def test_invalid_requests(protocol_client: TestClient, message: dict[str, Any]) -> None:
    assert protocol_client.post("/mcp", json=message).json()["error"]["code"] == -32600


@pytest.mark.parametrize(
    "status, body, is_error",
    [
        (200, {"success": True, "result": []}, False),
        (200, {"success": False, "results": [{"success": False, "error": "missing"}]}, True),
        (200, {"success": True, "results": [{"success": False, "error": "missing"}]}, True),
        (502, {"error": "UA unavailable"}, True),
    ],
)
def test_structured_tool_results(
    protocol_client: TestClient, monkeypatch: pytest.MonkeyPatch, status: int, body: dict[str, Any], is_error: bool
) -> None:
    async def invoke(*args: Any) -> dict[str, Any]:
        return {"status_code": status, "body": body}

    monkeypatch.setattr("i3x_server.api.mcp.monolithic.invoke_mcp_tool", invoke)
    tools = {tool["name"]: tool for tool in rpc(protocol_client, "tools/list")["result"]["tools"]}
    assert tools["readValues"]["annotations"]["readOnlyHint"] is True
    assert tools["readValues"]["outputSchema"]["properties"]["body"] == {
        "type": "object",
        "additionalProperties": True,
    }
    assert tools["createSubscription"]["annotations"]["readOnlyHint"] is False
    assert tools["syncSubscription"]["annotations"]["readOnlyHint"] is False
    for name in (
        "searchModelElements",
        "getElementContext",
        "summarizeSubtree",
        "traceRelationshipPath",
    ):
        assert tools[name]["annotations"]["readOnlyHint"] is True
    result = rpc(protocol_client, "tools/call", {"name": "readValues", "arguments": {"body": {}}})["result"]
    assert result["isError"] is is_error
    assert json.loads(result["content"][0]["text"]) == body
    assert result["structuredContent"] == {"statusCode": status, "body": body}
    validate(result["structuredContent"], tools["readValues"]["outputSchema"])

    modern_call = modern_rpc(
        protocol_client,
        "tools/call",
        {"name": "readValues", "arguments": {"body": {}}},
    )
    assert modern_call.status_code == 200
    modern_result = modern_call.json()["result"]
    assert modern_result["resultType"] == "complete"
    assert modern_result["_meta"][MCP_META_PROTOCOL_VERSION] == MCP_PROTOCOL_VERSION
    assert modern_result["isError"] is is_error


def test_mcp_object_and_value_results_are_compact() -> None:
    object_body = {
        "success": True,
        "result": [
            {
                "elementId": "asset-1",
                "displayName": "PLC_1",
                "typeElementId": "type-1",
                "parentId": None,
                "isComposition": False,
                "isExtended": False,
                "metadata": None,
            }
        ],
    }
    assert _compact_tool_body("getObjectsV1", object_body) == {
        "success": True,
        "result": [{"elementId": "asset-1", "displayName": "PLC_1"}],
    }

    related_body = {
        "success": True,
        "results": [
            {
                "success": True,
                "elementId": "asset-1",
                "result": [
                    {
                        "sourceRelationship": "HasComponent",
                        "object": {
                            "elementId": "property-1",
                            "displayName": "OrderNumber",
                            "typeElementId": "type-1",
                            "metadata": {"description": "Large optional details"},
                        },
                    }
                ],
            }
        ],
    }
    compacted_related = _compact_tool_body("queryRelatedObjectsV1", related_body)
    assert compacted_related["results"][0]["result"] == [
        {
            "sourceRelationship": "HasComponent",
            "object": {"elementId": "property-1", "displayName": "OrderNumber"},
        }
    ]

    value_body = {
        "success": True,
        "results": [
            {
                "success": True,
                "elementId": "property-1",
                "result": {
                    "isComposition": False,
                    "value": "123",
                    "quality": "Good",
                    "timestamp": "2026-10-21T11:00:00Z",
                    "components": None,
                },
            }
        ],
    }
    assert _compact_tool_body("queryLastKnownValuesV1", value_body)["results"][0]["result"] == {
        "value": "123",
        "quality": "Good",
        "timestamp": "2026-10-21T11:00:00Z",
    }


def test_tool_argument_error_is_protocol_error(protocol_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def invoke(*args: Any) -> dict[str, Any]:
        raise HTTPException(400, "Missing required body")

    monkeypatch.setattr("i3x_server.api.mcp.monolithic.invoke_mcp_tool", invoke)
    result = rpc(protocol_client, "tools/call", {"name": "readValues", "arguments": {"body": {}}})
    assert result["error"]["code"] == -32602


@pytest.mark.parametrize("request_id", [0, "request-1"])
def test_request_ids_are_preserved(protocol_client: TestClient, request_id: int | str) -> None:
    payload = protocol_client.post("/mcp", json={"jsonrpc": "2.0", "id": request_id, "method": "ping"}).json()
    assert payload == {"jsonrpc": "2.0", "id": request_id, "result": {}}


def test_origin_cannot_be_authorized_by_forged_host(protocol_client: TestClient) -> None:
    message = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert (
        protocol_client.post(
            "/mcp", json=message, headers={"Host": "evil.example", "Origin": "http://evil.example"}
        ).status_code
        == 403
    )
    assert (
        protocol_client.post(
            "/mcp", json=message, headers={"Host": "127.0.0.1:8000", "Origin": "http://127.0.0.1:8000"}
        ).status_code
        == 200
    )
    assert protocol_client.post("/mcp", json=message, headers={"Origin": "null"}).status_code == 403
    assert protocol_client.post("/mcp", json=message, headers={"Origin": "*"}).status_code == 403


def test_execution_exceptions_and_internal_errors(
    protocol_client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def unavailable(*args: Any) -> dict[str, Any]:
        raise HTTPException(503, "UA unavailable")

    monkeypatch.setattr("i3x_server.api.mcp.monolithic.invoke_mcp_tool", unavailable)
    params = {"name": "readValues", "arguments": {"body": {}}}
    result = rpc(protocol_client, "tools/call", params)["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["statusCode"] == 503

    async def broken(*args: Any) -> dict[str, Any]:
        raise RuntimeError("private implementation detail")

    monkeypatch.setattr("i3x_server.api.mcp.monolithic.invoke_mcp_tool", broken)
    error = rpc(protocol_client, "tools/call", params)["error"]
    assert error == {"code": -32603, "message": "Internal error"}
    assert "MCP request failed" in caplog.text


def test_invalid_arguments_do_not_invoke_tools(protocol_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: Any) -> dict[str, Any]:
        pytest.fail("Invalid arguments must not invoke the tool")

    monkeypatch.setattr("i3x_server.api.mcp.monolithic.invoke_mcp_tool", forbidden)
    for arguments in ({}, {"body": 42}, {"body": {}, "unknown": True}):
        assert (
            rpc(protocol_client, "tools/call", {"name": "readValues", "arguments": arguments})["error"]["code"]
            == -32602
        )
    assert rpc(protocol_client, "tools/call", {"name": "unknown"})["error"]["code"] == -32602
    assert rpc(protocol_client, "resources/read", {"uri": "i3x://unknown"})["error"]["code"] == -32002


@pytest.mark.parametrize("payload", [[], {}, {"status_code": "200", "body": {}}, {"status_code": 200}])
def test_invalid_tool_response_is_logged(
    protocol_client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, payload: Any
) -> None:
    async def invoke(*args: Any) -> Any:
        return payload

    monkeypatch.setattr("i3x_server.api.mcp.monolithic.invoke_mcp_tool", invoke)
    result = rpc(protocol_client, "tools/call", {"name": "readValues", "arguments": {"body": {}}})
    assert result["error"]["code"] == -32603
    assert "Invalid MCP tool response" in caplog.text
