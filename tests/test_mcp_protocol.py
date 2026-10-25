from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from jsonschema import validate

from i3x_server.api.mcp.monolithic import _compact_tool_body, router
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


def test_version_negotiation_and_ping(protocol_client: TestClient) -> None:
    result = rpc(protocol_client, "initialize", {"protocolVersion": "2099-01-01"})["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert "roots" not in result["capabilities"]
    assert result["instructions"]
    assert "getInfoV1 describes this i3X gateway" in result["instructions"]
    assert "never label an i3X ElementId as an OPC UA NodeId" in result["instructions"]
    assert "never claim the remote address space is complete" in result["instructions"]
    assert "Classify each request as OPC UA endpoint, i3X mapped-model, or mixed analysis" in result["instructions"]
    assert "i3x://guides/opcua-analysis" in result["instructions"]
    assert "separately labeled fields" in result["instructions"]
    assert rpc(protocol_client, "ping")["result"] == {}


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


def test_analysis_guides_are_listed_and_readable(protocol_client: TestClient) -> None:
    resources = rpc(protocol_client, "resources/list")["result"]["resources"]
    by_uri = {resource["uri"]: resource for resource in resources}
    opcua_uri = "i3x://guides/opcua-analysis"
    i3x_uri = "i3x://guides/i3x-model-analysis"
    assert by_uri[opcua_uri]["mimeType"] == "text/markdown"
    assert by_uri[i3x_uri]["mimeType"] == "text/markdown"

    opcua_content = rpc(protocol_client, "resources/read", {"uri": opcua_uri})["result"]["contents"][0]
    i3x_content = rpc(protocol_client, "resources/read", {"uri": i3x_uri})["result"]["contents"][0]
    assert "source NodeId returned or resolved" in opcua_content["text"]
    assert "exact i3X ElementId returned by the tool" in i3x_content["text"]


def test_http_contracts(protocol_client: TestClient) -> None:
    notification = protocol_client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert notification.status_code == 202
    assert notification.content == b""
    assert protocol_client.get("/mcp").status_code == 405
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
