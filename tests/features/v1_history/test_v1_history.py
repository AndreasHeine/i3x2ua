"""History query endpoints tests."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from i3x_server.api.v1.monolithic import _expanded_node_id
from i3x_server.infrastructure.opcua.client import OpcUaNamespaceInfo
from i3x_server.schemas.i3x import ModelNode


def test_v1_history_query(client: TestClient) -> None:
    response = client.post(
        "/v1/objects/history",
        json={
            "elementIds": ["property-abc"],
            "startTime": "2026-01-01T00:00:00Z",
            "endTime": "2026-01-02T00:00:00Z",
            "maxDepth": 1,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["results"][0]["success"] is True
    assert payload["results"][0]["result"]["isComposition"] is False
    assert len(payload["results"][0]["result"]["values"]) == 2


def test_v1_history_query_includes_component_histories_when_depth_allows(client: TestClient) -> None:
    from tests.conftest import fastapi_app

    app = fastapi_app(client)
    property_id = "property-abc"
    child_asset_id = "child-asset"
    child_prop_id = "child-prop"

    app.state.model_cache.nodes_by_id[child_asset_id] = ModelNode(
        id=child_asset_id,
        name="ChildAsset",
        kind="asset",
        type="ns=1;i=1001",
        children=[child_prop_id],
        source_node_id="ns=2;s=ChildAsset",
    )
    app.state.model_cache.nodes_by_id[child_prop_id] = ModelNode(
        id=child_prop_id,
        name="ChildProp",
        kind="property",
        type="i=11",
        children=[],
        source_node_id="ns=2;s=ChildProp",
    )
    app.state.model_cache.children_by_id[child_asset_id] = [child_prop_id]
    app.state.model_cache.hierarchy_children_by_id["asset-root"] = [property_id, child_asset_id]
    app.state.model_cache.composition_children_by_id["asset-root"] = [property_id]
    app.state.model_cache.composition_children_by_id[child_asset_id] = [child_prop_id]

    response = client.post(
        "/v1/objects/history",
        json={
            "elementIds": ["asset-root"],
            "startTime": "2026-01-01T00:00:00Z",
            "endTime": "2026-01-02T00:00:00Z",
            "maxDepth": 5,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    result = payload["results"][0]["result"]
    component_ids = set((result.get("components") or {}).keys())
    assert property_id in component_ids
    assert child_prop_id not in component_ids
    assert isinstance(result["components"][property_id]["values"], list)


def test_v1_history_query_serializes_binary_values(client: TestClient) -> None:
    from tests.conftest import fastapi_app

    fastapi_app(client).state.opcua_client.history_values["ns=2;s=Temperature"] = [
        SimpleNamespace(
            Value=SimpleNamespace(Value=b"\xff\x00"),
            StatusCode=SimpleNamespace(name="Good"),
            SourceTimestamp=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
            ServerTimestamp=None,
        )
    ]

    response = client.post(
        "/v1/objects/history",
        json={
            "elementIds": ["property-abc"],
            "startTime": "2026-01-01T00:00:00Z",
            "endTime": "2026-01-02T00:00:00Z",
            "maxDepth": 1,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["results"][0]["success"] is True
    assert payload["results"][0]["result"]["values"][0]["value"] == {
        "encoding": "base64",
        "data": base64.b64encode(b"\xff\x00").decode("ascii"),
    }


def test_expanded_node_id_does_not_rewrite_non_node_ids() -> None:
    namespaces = [OpcUaNamespaceInfo(uri="http://example.com/default", display_name="Default")]
    assert _expanded_node_id("asset-root", namespaces) == "asset-root"


def test_expanded_node_id_rewrites_node_id_strings() -> None:
    namespaces = [
        OpcUaNamespaceInfo(uri="http://example.com/default", display_name="Default"),
        OpcUaNamespaceInfo(uri="http://example.com/custom", display_name="Custom"),
    ]
    assert _expanded_node_id("ns=1;i=1001", namespaces) == "nsu=http://example.com/custom;i=1001"


def test_v1_history_query_missing_object(client: TestClient) -> None:
    response = client.post(
        "/v1/objects/history",
        json={
            "elementIds": ["missing"],
            "startTime": "2026-01-01T00:00:00Z",
            "endTime": "2026-01-02T00:00:00Z",
            "maxDepth": 1,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["results"][0]["success"] is False
    assert payload["results"][0]["error"]["code"] == 404


def test_v1_history_query_invalid_time_range(client: TestClient) -> None:
    response = client.post(
        "/v1/objects/history",
        json={
            "elementIds": ["property-abc"],
            "startTime": "2026-01-02T00:00:00Z",
            "endTime": "2026-01-01T00:00:00Z",
        },
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["code"] == 400
