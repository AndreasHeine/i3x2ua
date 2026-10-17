from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient
from jsonschema import validate
from pydantic import ValidationError

from i3x_server.application.services.asset_context import (
    AssetContextRequest,
    AssetContextService,
    AssetMonitoringRequest,
    AssetSearchRequest,
    AssetSnapshotRequest,
    DataAvailabilityRequest,
    SignalHistoryRequest,
)
from i3x_server.domain.ports.opcua import BoundedHistoryResult
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult
from tests.conftest import fastapi_app


def model() -> BuildResult:
    relationships = {
        "plant": {"HasChildren": ["pump", "pump2"]},
        "pump": {"HasComponent": ["temperature"], "HasParent": ["plant"], "Feeds": ["pump2"]},
        "pump2": {"HasComponent": ["temperature"], "Feeds": ["pump"]},
        "temperature": {"ComponentOf": ["pump", "pump2"]},
    }
    nodes = {
        element_id: ModelNode(
            id=element_id,
            name=element_id.title(),
            kind="property" if element_id == "temperature" else "asset",
            type="Double" if element_id == "temperature" else "PumpType",
            source_node_id=f"ns=2;s={element_id}",
            relationships=edges,
            metadata={"namespaceUri": "urn:plant"},
        )
        for element_id, edges in relationships.items()
    }
    return BuildResult(
        nodes_by_id=nodes,
        root_ids=["plant"],
        children_by_id={},
        instances_by_type_id={},
        property_to_node={"temperature": "ns=2;s=temperature"},
        action_to_method={},
        relationships_by_id=relationships,
        revision="revision-1",
    )


class Backend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_registration = False
        self.fail_cleanup = False
        self.slow_registration = False

    async def query(self, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((operation, args))
        ids = args.get("elementIds", [])
        if operation == "objects":
            return {
                "results": [
                    {
                        "elementId": element_id,
                        "success": True,
                        "result": {
                            "elementId": element_id,
                            "displayName": element_id.title(),
                            "typeElementId": "Double" if element_id == "temperature" else "PumpType",
                        },
                    }
                    for element_id in ids
                ]
            }
        if operation == "types":
            return {
                "results": [
                    {"elementId": element_id, "success": True, "result": {"schema": {"type": "number"}}}
                    for element_id in ids
                ]
            }
        if operation == "values":
            return {
                "results": [
                    {
                        "elementId": element_id,
                        "success": True,
                        "result": {
                            "value": 42,
                            "quality": "Good",
                            "timestamp": "2026-01-01T10:00:00Z",
                        },
                    }
                    for element_id in ids
                ]
            }
        if operation == "create":
            return {"success": True, "result": {"subscriptionId": "actual-handle"}}
        if operation == "register":
            if self.slow_registration:
                await asyncio.sleep(1)
            return {
                "success": not self.fail_registration,
                "results": [{"elementId": element_id, "success": not self.fail_registration} for element_id in ids],
            }
        if operation == "delete":
            return {"success": not self.fail_cleanup, "results": [{"success": not self.fail_cleanup}]}
        if operation == "history":
            return {
                "success": True,
                "truncated": True,
                "values": [
                    {"value": 10, "quality": "Good", "timestamp": "2026-01-01T10:00:00Z"},
                    {"value": 20, "quality": "Good", "timestamp": "2026-01-01T10:01:00Z"},
                    {"value": 100, "quality": "Bad", "timestamp": "2026-01-01T10:02:00Z"},
                    {"value": True, "quality": "Good", "timestamp": "2026-01-01T10:03:00Z"},
                ],
            }
        if operation in {"connection", "status", "capabilities"}:
            return {"success": True, "result": {"state": "Connected"}}
        raise AssertionError(operation)


@pytest.mark.asyncio
async def test_graph_preserves_cycles_shared_edges_and_schema_deduplication() -> None:
    backend = Backend()
    service = AssetContextService(model(), backend.query)
    result = await service.run(
        "discoverAssetContext",
        AssetContextRequest(
            elementIds=["plant"],
            maxDepth=3,
            includeGraph=True,
            includeValues=True,
        ),
    )
    assert result["success"] is True
    assert set(result["objects"]) == {"plant", "pump", "pump2", "temperature"}
    assert len(result["edges"]) == 9
    assert {edge["plane"] for edge in result["edges"]} == {"composition", "hierarchy", "graph"}
    assert len(result["types"]) == 2
    assert result["values"]["temperature"]["quality"] == "Good"
    assert result["values"]["temperature"]["timestamp"] == "2026-01-01T10:00:00Z"
    assert [args["elementIds"] for operation, args in backend.calls if operation == "types"] == [["Double", "PumpType"]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limits, reason",
    [
        ({"maxNodes": 2}, "maxNodes"),
        ({"maxDepth": 0}, "maxDepth"),
        ({"maxEdges": 1}, "maxEdges"),
    ],
)
async def test_traversal_limits_are_measured(limits: dict[str, int], reason: str) -> None:
    result = await AssetContextService(model(), Backend().query).run(
        "discoverAssetContext", AssetContextRequest.model_validate({"elementIds": ["plant"], **limits})
    )
    assert result["success"] is True
    assert result["coverage"]["truncated"] is True
    assert reason in result["coverage"]["limitReasons"]
    assert len(result["objects"]) <= limits.get("maxNodes", 100)
    assert len(result["edges"]) <= limits.get("maxEdges", 500)


@pytest.mark.asyncio
async def test_missing_ids_and_no_hierarchy_snapshot() -> None:
    service = AssetContextService(model(), Backend().query)
    missing = await service.run("discoverAssetContext", AssetContextRequest(elementIds=["missing"]))
    assert missing["success"] is False
    assert missing["errors"][0]["elementId"] == "missing"
    snapshot = await service.run("getAssetSnapshot", AssetSnapshotRequest(elementIds=["plant"], includeHierarchy=False))
    assert snapshot["values"] == {}
    snapshot = await service.run("getAssetSnapshot", AssetSnapshotRequest(elementIds=["plant"], includeValues=False))
    assert snapshot["values"]["temperature"]["value"] == 42
    assert snapshot["atomicSnapshot"] is False
    assert snapshot["collectionStartedAt"] <= snapshot["collectionCompletedAt"]


@pytest.mark.asyncio
async def test_search_returns_candidates_and_revision_checked_paging() -> None:
    service = AssetContextService(model(), Backend().query)
    result = await service.run(
        "findAssets",
        AssetSearchRequest(
            name="pump",
            namespaceUri="urn:plant",
            typeElementId="PumpType",
            maxNodes=1,
        ),
    )
    assert result["candidates"][0]["elementId"] == "pump"
    offset = result["coverage"]["nextOffset"]
    assert offset == 2
    assert result["success"] is True
    assert result["coverage"]["truncated"] is True
    next_page = await service.run(
        "findAssets",
        AssetSearchRequest(
            name="pump",
            maxNodes=1,
            offset=offset,
            modelRevision="revision-1",
        ),
    )
    assert next_page["candidates"][0]["elementId"] == "pump2"
    stale = await service.run("findAssets", AssetSearchRequest(modelRevision="old"))
    assert stale["success"] is False
    bounded = await service.run("findAssets", AssetSearchRequest(maxScan=1))
    assert bounded["coverage"]["scannedNodes"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("registration_failure, cleanup_failure", [(False, False), (True, False), (True, True)])
async def test_monitoring_lifecycle_and_cleanup(registration_failure: bool, cleanup_failure: bool) -> None:
    backend = Backend()
    backend.fail_registration = registration_failure
    backend.fail_cleanup = cleanup_failure
    result = await AssetContextService(model(), backend.query).run(
        "startAssetMonitoring", AssetMonitoringRequest(elementIds=["pump"], clientId="not-a-credential")
    )
    assert result["subscriptionId"] == "actual-handle"
    assert result["success"] is not registration_failure
    assert result["monitoringActive"] is (None if cleanup_failure else not registration_failure)
    registration = next(args for operation, args in backend.calls if operation == "register")
    assert registration["elementIds"] == ["temperature"]
    assert registration["maxDepth"] == 1
    assert any(operation == "delete" for operation, _ in backend.calls) is registration_failure
    assert any(item["stage"] == "cleanup" for item in result["errors"]) is cleanup_failure
    assert not any(operation in {"sync", "acknowledge"} for operation, _ in backend.calls)


@pytest.mark.asyncio
async def test_monitoring_refuses_truncated_or_missing_targets_before_creation() -> None:
    backend = Backend()
    service = AssetContextService(model(), backend.query)
    for ids, depth in [(["pump"], 0), (["missing"], 2), (["plant"], 2)]:
        result = await service.run(
            "startAssetMonitoring", AssetMonitoringRequest(elementIds=ids, clientId="c", maxDepth=depth)
        )
        assert result["success"] is False
    assert backend.calls == []


@pytest.mark.asyncio
async def test_monitoring_timeout_returns_handle_and_cleanup_evidence() -> None:
    backend = Backend()
    backend.slow_registration = True
    result = await AssetContextService(model(), backend.query).run(
        "startAssetMonitoring", AssetMonitoringRequest(elementIds=["pump"], clientId="c", timeoutSeconds=0.01)
    )
    assert result["success"] is False
    assert result["subscriptionId"] == "actual-handle"
    assert result["cleanup"]["success"] is True
    assert result["errors"][-1]["stage"] == "timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["create", "register", "delete", "missingRegistration", "slowCreate"])
async def test_monitoring_compensation_preserves_uncertain_outcomes(failure: str) -> None:
    backend = Backend()

    async def query(operation: str, args: dict[str, Any]) -> dict[str, Any]:
        if operation == failure:
            raise RuntimeError(f"Injected {failure} failure")
        if operation == "create" and failure == "slowCreate":
            await asyncio.sleep(0.03)
        if operation == "register" and failure in {"delete", "missingRegistration"}:
            return {"success": True, "results": []}
        return await backend.query(operation, args)

    result = await AssetContextService(model(), query).run(
        "startAssetMonitoring",
        AssetMonitoringRequest(
            elementIds=["pump"], clientId="c", timeoutSeconds=0.01 if failure == "slowCreate" else 1
        ),
    )
    assert result["success"] is False
    assert result["subscriptionId"] == (None if failure == "create" else "actual-handle")
    assert result["monitoringActive"] is (None if failure == "delete" else False)
    if failure not in {"create", "delete"}:
        assert result["cleanup"]["success"] is True
    if failure == "delete":
        assert any(error["stage"] == "cleanup" for error in result["errors"])


@pytest.mark.asyncio
async def test_bulk_missing_results_batch_sizes_and_output_budget() -> None:
    backend = Backend()
    mapped = model()
    mapped.nodes_by_id = {
        str(index): ModelNode(
            id=str(index), name=str(index), kind="property", type="Double", source_node_id=f"ns=2;s={index}"
        )
        for index in range(100)
    }
    service = AssetContextService(mapped, backend.query)
    result = await service.run(
        "getAssetSnapshot", AssetSnapshotRequest(elementIds=list(mapped.nodes_by_id), maxDepth=0)
    )
    assert result["success"] is True
    assert len(result["values"]) == 100
    assert [len(args["elementIds"]) for operation, args in backend.calls if operation == "values"] == [50, 50]

    async def missing(operation: str, args: dict[str, Any]) -> dict[str, Any]:
        if operation == "objects":
            return {"success": False, "results": []}
        return await backend.query(operation, args)

    result = await AssetContextService(model(), missing).run(
        "discoverAssetContext", AssetContextRequest(elementIds=["temperature"])
    )
    assert result["success"] is False
    assert result["errors"][0]["elementId"] == "temperature"

    async def oversized(operation: str, args: dict[str, Any]) -> dict[str, Any]:
        payload = await backend.query(operation, args)
        if operation == "register":
            payload["extra"] = "x" * 1_048_576
        return payload

    result = await AssetContextService(model(), oversized).run(
        "startAssetMonitoring", AssetMonitoringRequest(elementIds=["pump"], clientId="c")
    )
    assert result["errors"][0]["stage"] == "output"
    assert result["coverage"]["limitReasons"] == ["maxOutputBytes"]
    assert result["subscriptionId"] == "actual-handle"
    assert result["monitoringActive"] is True


@pytest.mark.asyncio
async def test_edge_budget_across_relationships_and_scoped_search() -> None:
    mapped = model()
    mapped.nodes_by_id["temperature"].relationships = {}
    mapped.relationships_by_id["temperature"] = {}
    service = AssetContextService(mapped, Backend().query)
    result = await service.run("discoverAssetContext", AssetContextRequest(elementIds=["pump"], maxEdges=1))
    assert result["success"] is True
    assert result["coverage"]["limitReasons"] == ["maxEdges"]
    scoped = await service.run(
        "findAssets", AssetSearchRequest(scopeElementIds=["pump"], name="Temperature", maxNodes=4)
    )
    assert [item["elementId"] for item in scoped["candidates"]] == ["temperature"]
    assert scoped["coverage"]["continuationSupported"] is False
    assert scoped["coverage"]["nextOffset"] is None


@pytest.mark.asyncio
async def test_history_statistics_preserve_quality_and_explicit_truncation() -> None:
    backend = Backend()
    result = await AssetContextService(model(), backend.query).run(
        "summarizeSignalHistory",
        SignalHistoryRequest.model_validate(
            {
                "elementIds": ["pump"],
                "startTime": "2026-01-01T10:00:00Z",
                "endTime": "2026-01-01T11:00:00Z",
                "maxNodes": 10,
                "maxPointsPerSignal": 10,
            }
        ),
    )
    summary = result["history"]["temperature"]
    assert summary["arithmeticMean"] == 15
    assert summary["numericGoodSampleCount"] == 2
    assert summary["qualityCounts"] == {"Good": 3, "Bad": 1}
    assert summary["goodSampleFraction"] == 0.75
    assert summary["truncated"] is True
    assert result["success"] is True
    assert result["coverage"]["truncated"] is True
    assert next(args for operation, args in backend.calls if operation == "history")["maxPoints"] == 10
    assert not any(operation == "values" for operation, _ in backend.calls)


@pytest.mark.asyncio
async def test_diagnostics_report_timestamp_evidence_not_health_verdict() -> None:
    result = await AssetContextService(model(), Backend().query).run(
        "diagnoseDataAvailability", DataAvailabilityRequest(elementIds=["pump"])
    )
    assert result["findings"][0]["kind"] == "oldTimestamp"
    assert set(result["diagnostics"]) == {"connection", "status", "capabilities"}
    assert "unchanged signal" in result["interpretation"]


@pytest.mark.parametrize(
    "args",
    [
        {"elementIds": [""]},
        {"elementIds": ["bad "]},
        {"elementIds": ["a", "b"], "maxNodes": 1},
        {"elementIds": ["a"], "maxDepth": -1},
        {"elementIds": ["a"], "maxNodes": 501},
    ],
)
def test_request_boundaries(args: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        AssetContextRequest(**args)


def test_history_rejects_unbounded_or_naive_intervals() -> None:
    for start, end in [
        ("2026-01-01T10:00:00", "2026-01-01T11:00:00Z"),
        ("2026-01-02T10:00:00Z", "2026-01-01T11:00:00Z"),
        ("2026-01-01T10:00:00Z", "2026-01-03T11:00:00Z"),
    ]:
        with pytest.raises(ValidationError):
            SignalHistoryRequest.model_validate({"elementIds": ["a"], "startTime": start, "endTime": end})


def test_search_rejects_scope_budget_overflow_and_unsupported_continuation() -> None:
    for arguments in (
        {"scopeElementIds": ["a", "b"], "maxNodes": 1},
        {"scopeElementIds": ["a"], "offset": 1},
    ):
        with pytest.raises(ValidationError):
            AssetSearchRequest.model_validate(arguments)


@pytest.mark.parametrize("tool", ["discoverAssetContext", "getAssetSnapshot", "diagnoseDataAvailability"])
def test_bounded_discovery_is_not_an_mcp_execution_error(client: TestClient, tool: str) -> None:
    app = fastapi_app(client)
    app.state.model_cache.nodes_by_id["asset-root"].relationships = {"HasComponent": ["property-abc"]}

    def call(element_id: str) -> dict[str, Any]:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": tool,
                    "arguments": {
                        "elementIds": [element_id],
                        "maxDepth": 0,
                        "includeValues": True,
                        "includeSchemas": False,
                    },
                },
            },
        )
        assert response.status_code == 200
        result = response.json()["result"]
        assert isinstance(result, dict)
        return result

    result = call("asset-root")
    body = result["structuredContent"]["body"]
    assert result["isError"] is False
    assert body["success"] is True
    assert body["errors"] == []
    assert body["coverage"]["truncated"] is True
    assert body["coverage"]["limitReasons"] == ["maxDepth"]
    assert "asset-root" in body["objects"]

    missing = call("missing")
    assert missing["isError"] is True
    assert missing["structuredContent"]["body"]["success"] is False


def test_model_timeout_returns_collection_evidence(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from i3x_server.api.mcp import composites

    async def slow_model(request: Any) -> BuildResult:
        await asyncio.sleep(1)
        return model()

    monkeypatch.setattr(composites, "get_or_build_model", slow_model)
    result = client.post(
        "/mcp/composites/snapshot", json={"elementIds": ["property-abc"], "timeoutSeconds": 0.01}
    ).json()
    assert result["success"] is False
    assert result["errors"][0]["stage"] == "model"
    assert result["collectionStartedAt"] <= result["collectionCompletedAt"]
    assert result["atomicSnapshot"] is False


def test_mcp_catalog_calls_and_existing_subscription_handles(client: TestClient) -> None:
    app = fastapi_app(client)
    mapped = app.state.model_cache
    mapped.nodes_by_id["asset-root"].relationships = {"HasComponent": ["property-abc"]}
    tools = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]
    by_name = {item["name"]: item for item in tools}
    names = {
        "discoverAssetContext",
        "findAssets",
        "getAssetSnapshot",
        "startAssetMonitoring",
        "summarizeSignalHistory",
        "diagnoseDataAvailability",
    }
    assert names <= by_name.keys()
    assert by_name["getAssetSnapshot"]["annotations"]["readOnlyHint"] is True
    assert by_name["startAssetMonitoring"]["annotations"]["readOnlyHint"] is False
    assert "body" not in by_name["discoverAssetContext"]["inputSchema"]["properties"]
    result = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "getAssetSnapshot", "arguments": {"elementIds": ["asset-root"]}},
        },
    ).json()["result"]
    assert (
        result["structuredContent"]["body"]["values"]["property-abc"]["value"]
        == app.state.opcua_client.values["ns=2;s=Temperature"]
    )
    validate(result["structuredContent"], by_name["getAssetSnapshot"]["outputSchema"])
    created = client.post(
        "/mcp/call",
        json={
            "tool": "startAssetMonitoring",
            "arguments": {"elementIds": ["asset-root"], "clientId": "composite-test"},
        },
    )
    assert created.status_code == 200
    assert created.json()["success"] is True
    handle = created.json()["subscriptionId"]
    deleted = client.post(
        "/v1/subscriptions/delete",
        json={
            "clientId": "composite-test",
            "subscriptionIds": [handle],
        },
    )
    assert deleted.json()["results"][0]["success"] is True
    unsupported = client.post(
        "/mcp/call",
        json={
            "tool": "summarizeSignalHistory",
            "arguments": {
                "elementIds": ["property-abc"],
                "startTime": "2026-01-01T10:00:00Z",
                "endTime": "2026-01-01T11:00:00Z",
            },
        },
    )
    assert unsupported.json()["success"] is False
    assert "unsupported" in unsupported.json()["errors"][0]["message"]


def test_history_adapter_preserves_timestamp_origin(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timezone

    from asyncua import ua

    async def read(node_id: str, start: datetime, end: datetime, max_points: int) -> BoundedHistoryResult:
        return BoundedHistoryResult(
            values=[
                ua.DataValue(ua.Variant(1), SourceTimestamp=ua.DateTime.fromisoformat(start.isoformat())),
                ua.DataValue(ua.Variant(2), ServerTimestamp=ua.DateTime.fromisoformat(end.isoformat())),
                ua.DataValue(ua.Variant(3)),
            ],
            truncated=False,
        )

    monkeypatch.setattr(fastapi_app(client).state.opcua_client, "read_bounded_history", read, raising=False)
    result = client.post(
        "/mcp/composites/history",
        json={
            "elementIds": ["property-abc"],
            "startTime": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
            "endTime": datetime(2026, 1, 1, 1, tzinfo=timezone.utc).isoformat(),
        },
    ).json()
    assert result["success"] is True
    history = result["history"]["property-abc"]
    assert history["sampleCount"] == 3
    assert history["arithmeticMean"] == 2
    assert history["first"]["timestamp"] is None
    assert history["first"]["timestampKind"] == "unavailable"
    assert history["last"]["timestampKind"] == "server"
