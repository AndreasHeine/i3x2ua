"""History query endpoints tests."""

from __future__ import annotations

import base64
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from i3x_server.api.v1.object_value_routes import _COMPOSITION_TRUNCATION_DETAIL
from i3x_server.domain.utils import expanded_node_id
from i3x_server.infrastructure.opcua.client import OpcUaNamespaceInfo
from i3x_server.schemas.i3x import ModelNode
from tests.conftest import (
    add_composition_depth_chain,
    assert_documented_response,
    data_value_dto,
    fastapi_app,
)


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

    # ChildProp has history data, but it hangs on a hierarchical child (HasChildren):
    # composition recursion must not collect it via the parent (spec :1111/:1924).
    app.state.opcua_client.history_values["ns=2;s=ChildProp"] = [
        data_value_dto(5.0, "Good", datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc))
    ]

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


def test_v1_history_max_depth_counts_composition_levels(client: TestClient) -> None:
    """`maxDepth = N` recurses N composition levels for history as well (spec :1105-1110)."""
    app = fastapi_app(client)
    add_composition_depth_chain(app)

    expected_by_depth = {
        1: set(),
        2: {"comp-prop", "mid-prop"},
        3: {"comp-prop", "mid-prop", "deep-prop"},
        0: {"comp-prop", "mid-prop", "deep-prop"},
    }
    for depth, expected_ids in expected_by_depth.items():
        response = client.post(
            "/v1/objects/history",
            json={
                "elementIds": ["asset-root"],
                "startTime": "2026-01-01T00:00:00Z",
                "endTime": "2026-01-02T00:00:00Z",
                "maxDepth": depth,
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        result = payload["results"][0]["result"]
        components = result.get("components") or {}
        assert set(components) == expected_ids, f"maxDepth={depth}"
        # Hierarchy-only children stay independent; they need their own history request.
        assert "hier-prop" not in components
        assert "hier-child" not in components
        for entry in components.values():
            assert len(entry["values"]) == 1
            assert entry["values"][0]["quality"] == "Good"


def test_v1_history_depth_cap_returns_206_with_response_detail(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """History recursion obeys the server caps and reports them as HTTP 206 (spec :1113-1119)."""
    app = fastapi_app(client)
    add_composition_depth_chain(app)
    monkeypatch.setenv("I3X_COMPOSITION_MAX_DEPTH", "2")

    response = client.post(
        "/v1/objects/history",
        json={
            "elementIds": ["asset-root"],
            "startTime": "2026-01-01T00:00:00Z",
            "endTime": "2026-01-02T00:00:00Z",
            "maxDepth": 0,
        },
    )
    assert response.status_code == 206
    payload = response.json()
    assert payload["success"] is True
    assert set(payload["results"][0]["result"]["components"]) == {"comp-prop", "mid-prop"}
    assert payload["responseDetail"] == {
        "title": "Partial Content",
        "status": 206,
        "detail": _COMPOSITION_TRUNCATION_DETAIL,
    }
    assert_documented_response(client, "post", "/v1/objects/history", 206, payload)


def test_v1_history_node_budget_returns_206(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    app = fastapi_app(client)
    add_composition_depth_chain(app)
    monkeypatch.setenv("I3X_COMPOSITION_MAX_NODES", "1")

    response = client.post(
        "/v1/objects/history",
        json={
            "elementIds": ["asset-root"],
            "startTime": "2026-01-01T00:00:00Z",
            "endTime": "2026-01-02T00:00:00Z",
            "maxDepth": 0,
        },
    )
    assert response.status_code == 206
    payload = response.json()
    assert set(payload["results"][0]["result"]["components"]) == {"comp-prop"}
    assert payload["responseDetail"]["status"] == 206


def test_v1_history_without_stored_data_returns_goodnodata(client: TestClient) -> None:
    """Spec :1294: a platform without stored history answers the range with GoodNoData, not an error."""
    app = fastapi_app(client)
    app.state.opcua_client.history_values = {}

    response = client.post(
        "/v1/objects/history",
        json={
            "elementIds": ["property-abc"],
            "startTime": "2026-01-01T00:00:00Z",
            "endTime": "2026-01-02T00:00:00Z",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    item = payload["results"][0]
    assert item["success"] is True
    assert item["result"]["isComposition"] is False
    assert item["result"]["values"] == [{"value": None, "quality": "GoodNoData", "timestamp": "2026-01-02T00:00:00Z"}]


def test_v1_history_composition_root_without_own_history_still_succeeds(client: TestClient) -> None:
    """`maxDepth = 1` on a container is a valid query; absence of history is not an item error."""
    app = fastapi_app(client)
    app.state.opcua_client.history_values = {}

    response = client.post(
        "/v1/objects/history",
        json={
            "elementIds": ["asset-root"],
            "startTime": "2026-01-01T00:00:00Z",
            "endTime": "2026-01-02T00:00:00Z",
            "maxDepth": 1,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    result = payload["results"][0]["result"]
    assert result["values"] == [{"value": None, "quality": "GoodNoData", "timestamp": "2026-01-02T00:00:00Z"}]
    assert result.get("components") is None


def test_v1_history_component_without_data_reports_goodnodata_per_component(client: TestClient) -> None:
    app = fastapi_app(client)
    add_composition_depth_chain(app)
    app.state.opcua_client.history_values = {"ns=2;s=CompositionProp": []}

    response = client.post(
        "/v1/objects/history",
        json={
            "elementIds": ["asset-root"],
            "startTime": "2026-01-01T00:00:00Z",
            "endTime": "2026-01-02T00:00:00Z",
            "maxDepth": 2,
        },
    )
    assert response.status_code == 200
    result = response.json()["results"][0]["result"]
    assert set(result["components"]) == {"comp-prop", "mid-prop"}
    assert result["components"]["comp-prop"]["values"] == [
        {"value": None, "quality": "GoodNoData", "timestamp": "2026-01-02T00:00:00Z"}
    ]
    assert result["components"]["mid-prop"]["values"] == [
        {"value": None, "quality": "GoodNoData", "timestamp": "2026-01-02T00:00:00Z"}
    ]


def test_v1_history_query_serializes_binary_values(client: TestClient) -> None:
    fastapi_app(client).state.opcua_client.history_values["ns=2;s=Temperature"] = [
        data_value_dto(b"\xff\x00", "Good", datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc))
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
    assert payload["results"][0]["result"]["values"][0]["value"] == base64.b64encode(b"\xff\x00").decode("ascii")


def test_expanded_node_id_does_not_rewrite_non_node_ids() -> None:
    namespaces = [OpcUaNamespaceInfo(uri="http://example.com/default", display_name="Default")]
    assert expanded_node_id("asset-root", namespaces) == "asset-root"


def test_expanded_node_id_rewrites_node_id_strings() -> None:
    namespaces = [
        OpcUaNamespaceInfo(uri="http://example.com/default", display_name="Default"),
        OpcUaNamespaceInfo(uri="http://example.com/custom", display_name="Custom"),
    ]
    assert expanded_node_id("ns=1;i=1001", namespaces) == "nsu=http://example.com/custom;i=1001"


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


def test_v1_history_query_requires_timezone(client: TestClient) -> None:
    response = client.post(
        "/v1/objects/history",
        json={
            "elementIds": ["property-abc"],
            "startTime": "2026-01-01T00:00:00",
            "endTime": "2026-01-02T00:00:00Z",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == 400


def test_v1_history_query_accepts_rfc3339_offset(client: TestClient) -> None:
    response = client.post(
        "/v1/objects/history",
        json={
            "elementIds": ["property-abc"],
            "startTime": "2026-01-01T02:00:00+02:00",
            "endTime": "2026-01-02T02:00:00+02:00",
        },
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["success"] is True
