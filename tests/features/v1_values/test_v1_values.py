"""Value query endpoints tests."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from i3x_server.api.v1.object_value_routes import _COMPOSITION_TRUNCATION_DETAIL
from i3x_server.schemas.i3x import ModelNode
from tests.conftest import (
    FakeExtensionObject,
    FakeMachineConfig,
    FakeMachineThresholds,
    add_composition_depth_chain,
    assert_documented_response,
    fastapi_app,
)


def test_v1_value_missing_element_item_includes_response_detail(client: TestClient) -> None:
    response = client.post(
        "/v1/objects/value",
        json={"elementIds": ["property-abc", "does-not-exist"], "maxDepth": 1},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    failing = next(item for item in payload["results"] if not item["success"])
    assert failing["elementId"] == "does-not-exist"
    assert failing["error"]["code"] == 404
    assert failing["responseDetail"]["status"] == 404
    assert failing["responseDetail"]["title"] == "Not Found"


def test_v1_is_composition_true_when_composition_children_exist(client: TestClient) -> None:
    app = fastapi_app(client)
    app.state.model_cache.composition_children_by_id = {"asset-root": ["property-abc"]}

    response = client.post(
        "/v1/objects/value",
        json={"elementIds": ["asset-root"], "maxDepth": 1},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["results"][0]["success"] is True
    assert payload["results"][0]["result"]["isComposition"] is True


def test_v1_value_query_container_node_returns_goodnodata_without_direct_read(client: TestClient) -> None:
    app = fastapi_app(client)
    called = False

    async def read_data_values(node_ids: list[str]) -> list[Any]:
        nonlocal called
        called = True
        assert "ns=2;s=Machine" not in node_ids
        return []

    app.state.opcua_client.read_data_values = read_data_values

    response = client.post(
        "/v1/objects/value",
        json={"elementIds": ["asset-root"], "maxDepth": 1},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert called is False

    result = payload["results"][0]["result"]
    assert result["value"] is None
    assert result["quality"] == "GoodNoData"


def test_v1_short_upstream_batch_is_reported_as_an_upstream_error_not_good_nodata(client: TestClient) -> None:
    async def read_data_values(node_ids: list[str]) -> list[Any]:
        assert node_ids == ["ns=2;s=Temperature"]
        return []

    fastapi_app(client).state.opcua_client.read_data_values = read_data_values

    response = client.post(
        "/v1/objects/value",
        json={"elementIds": ["property-abc"], "maxDepth": 1},
    )
    assert response.status_code == 502
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == 502
    assert payload["error"]["message"] == "Failed to read values"
    assert payload["responseDetail"]["status"] == 502
    assert_documented_response(client, "post", "/v1/objects/value", 502, payload)


def test_v1_is_composition_false_when_no_composition_children(client: TestClient) -> None:
    app = fastapi_app(client)
    app.state.model_cache.composition_children_by_id = {}
    response = client.post(
        "/v1/objects/value",
        json={"elementIds": ["property-abc"], "maxDepth": 1},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["results"][0]["success"] is True
    assert payload["results"][0]["result"]["isComposition"] is False


def test_v1_value_recursion_uses_composition_not_hierarchy(client: TestClient) -> None:
    app = fastapi_app(client)
    property_id = "property-abc"
    child_asset_id = "asset-child"
    child_prop_id = "child-prop"

    app.state.model_cache.nodes_by_id[child_asset_id] = ModelNode(
        id=child_asset_id,
        name="ChildAsset",
        kind="asset",
        type=None,
        children=[child_prop_id],
        source_node_id="ns=2;s=ChildAsset",
    )
    app.state.model_cache.nodes_by_id[child_prop_id] = ModelNode(
        id=child_prop_id,
        name="ChildProp",
        kind="property",
        type="ns=1;i=11",
        children=[],
        source_node_id="ns=2;s=ChildProp",
    )
    app.state.model_cache.property_to_node[child_prop_id] = "ns=2;s=ChildProp"
    app.state.model_cache.children_by_id[child_prop_id] = []
    app.state.model_cache.children_by_id[child_asset_id] = [child_prop_id]

    # asset-root has child-asset-id in hierarchy but NOT in composition
    app.state.model_cache.hierarchy_children_by_id["asset-root"] = [property_id, "action-def", child_asset_id]
    app.state.model_cache.composition_children_by_id["asset-root"] = [property_id]
    app.state.model_cache.composition_children_by_id[child_asset_id] = []

    app.state.opcua_client.values["ns=2;s=ChildProp"] = 5.0

    response = client.post(
        "/v1/objects/value",
        json={"elementIds": ["asset-root"], "maxDepth": 5},
    )
    assert response.status_code == 200
    payload = response.json()
    result = payload["results"][0]["result"]
    component_ids = set((result.get("components") or {}).keys())
    assert property_id in component_ids
    assert child_prop_id not in component_ids


def test_v1_value_max_depth_counts_composition_levels(client: TestClient) -> None:
    """`maxDepth = N` collects composition descendants up to N levels (spec :1105-1110)."""
    app = fastapi_app(client)
    add_composition_depth_chain(app)

    expected_by_depth = {
        1: set(),
        2: {"comp-prop", "mid-prop"},
        3: {"comp-prop", "mid-prop", "deep-prop"},
        0: {"comp-prop", "mid-prop", "deep-prop"},
    }
    for depth, expected_ids in expected_by_depth.items():
        response = client.post("/v1/objects/value", json={"elementIds": ["asset-root"], "maxDepth": depth})
        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        result = payload["results"][0]["result"]
        components = result.get("components") or {}
        assert set(components) == expected_ids, f"maxDepth={depth}"
        # The hierarchy branch (HasChildren) is independent and never read via the parent.
        assert "hier-prop" not in components
        assert "hier-child" not in components


def test_v1_value_query_dedups_repeated_element_ids(client: TestClient) -> None:
    """A repeated elementId must not double the upstream read (history dedups node ids the same way)."""
    requested: list[list[str]] = []

    async def read_data_values(node_ids: list[str]) -> list[Any]:
        requested.append(list(node_ids))
        return [
            SimpleNamespace(
                Value=SimpleNamespace(Value=23.5),
                StatusCode=SimpleNamespace(name="Good", is_good=lambda: True),
                SourceTimestamp=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                ServerTimestamp=None,
            )
            for _ in node_ids
        ]

    fastapi_app(client).state.opcua_client.read_data_values = read_data_values

    response = client.post(
        "/v1/objects/value",
        json={"elementIds": ["property-abc", "property-abc"], "maxDepth": 1},
    )
    assert response.status_code == 200
    payload = response.json()
    assert requested == [["ns=2;s=Temperature"]]
    assert [item["result"]["value"] for item in payload["results"]] == [23.5, 23.5]
    assert_documented_response(client, "post", "/v1/objects/value", 200, payload)


def test_v1_value_query_dedups_nodes_shared_by_several_elements(client: TestClient) -> None:
    """A property that is also a composition child of another requested element is read once."""
    app = fastapi_app(client)
    add_composition_depth_chain(app)
    requested: list[list[str]] = []

    async def read_data_values(node_ids: list[str]) -> list[Any]:
        requested.append(list(node_ids))
        return [
            SimpleNamespace(
                Value=SimpleNamespace(Value=float(len(node_ids))),
                StatusCode=SimpleNamespace(name="Good", is_good=lambda: True),
                SourceTimestamp=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                ServerTimestamp=None,
            )
            for _ in node_ids
        ]

    app.state.opcua_client.read_data_values = read_data_values

    response = client.post(
        "/v1/objects/value",
        json={"elementIds": ["asset-root", "comp-prop"], "maxDepth": 2},
    )
    assert response.status_code == 200
    payload = response.json()
    assert requested == [["ns=2;s=CompositionProp", "ns=2;s=MidProp"]]
    assert payload["results"][0]["result"]["components"]["comp-prop"]["value"] == 2.0
    assert payload["results"][1]["result"]["value"] == 2.0


def test_v1_value_depth_cap_returns_206_with_response_detail(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Server limits must not be reported as a complete result (spec IMPLEMENTATION_GUIDE.md:1113-1119)."""
    app = fastapi_app(client)
    add_composition_depth_chain(app)
    monkeypatch.setenv("I3X_COMPOSITION_MAX_DEPTH", "2")

    response = client.post("/v1/objects/value", json={"elementIds": ["asset-root"], "maxDepth": 0})
    assert response.status_code == 206
    payload = response.json()
    assert payload["success"] is True
    components = payload["results"][0]["result"]["components"]
    assert set(components) == {"comp-prop", "mid-prop"}
    assert payload["responseDetail"] == {
        "title": "Partial Content",
        "status": 206,
        "detail": _COMPOSITION_TRUNCATION_DETAIL,
    }
    assert_documented_response(client, "post", "/v1/objects/value", 206, payload)


def test_v1_value_depth_cap_not_truncated_when_request_is_within_cap(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = fastapi_app(client)
    add_composition_depth_chain(app)
    monkeypatch.setenv("I3X_COMPOSITION_MAX_DEPTH", "2")

    response = client.post("/v1/objects/value", json={"elementIds": ["asset-root"], "maxDepth": 2})
    assert response.status_code == 200
    payload = response.json()
    assert payload["responseDetail"] is None
    assert set(payload["results"][0]["result"]["components"]) == {"comp-prop", "mid-prop"}


def test_v1_value_node_budget_returns_206(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    app = fastapi_app(client)
    add_composition_depth_chain(app)
    monkeypatch.setenv("I3X_COMPOSITION_MAX_NODES", "1")

    response = client.post("/v1/objects/value", json={"elementIds": ["asset-root"], "maxDepth": 0})
    assert response.status_code == 206
    payload = response.json()
    assert set(payload["results"][0]["result"]["components"]) == {"comp-prop"}
    assert payload["responseDetail"]["status"] == 206


def test_v1_value_query_serializes_binary_values(client: TestClient) -> None:
    fastapi_app(client).state.opcua_client.values["ns=2;s=Temperature"] = b"\xff\x00"

    response = client.post(
        "/v1/objects/value",
        json={
            "elementIds": ["property-abc"],
            "maxDepth": 1,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["results"][0]["success"] is True
    assert payload["results"][0]["result"]["value"] == base64.b64encode(b"\xff\x00").decode("ascii")


def test_v1_value_query_serializes_structured_object_arrays(client: TestClient) -> None:
    async def read_data_values(node_ids: list[str]) -> list[Any]:
        assert node_ids == ["ns=2;s=Temperature"]
        value = [
            FakeExtensionObject(
                "ns=1;i=3001",
                FakeMachineConfig(
                    thresholds=FakeMachineThresholds(min=10.0, max=120.5),
                    mode="auto",
                ),
            ),
            FakeExtensionObject(
                "ns=1;i=3001",
                FakeMachineConfig(
                    thresholds=FakeMachineThresholds(min=12.0, max=130.0),
                    mode="manual",
                ),
            ),
        ]
        return [
            SimpleNamespace(
                Value=SimpleNamespace(Value=value),
                StatusCode=SimpleNamespace(name="Good", is_good=lambda: True),
                SourceTimestamp=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                ServerTimestamp=None,
            )
        ]

    fastapi_app(client).state.opcua_client.read_data_values = read_data_values

    response = client.post(
        "/v1/objects/value",
        json={
            "elementIds": ["property-abc"],
            "maxDepth": 1,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["results"][0]["success"] is True
    assert payload["results"][0]["result"]["value"] == [
        {
            "TypeId": "ns=1;i=3001",
            "Body": {
                "thresholds": {"min": 10.0, "max": 120.5},
                "mode": "auto",
            },
        },
        {
            "TypeId": "ns=1;i=3001",
            "Body": {
                "thresholds": {"min": 12.0, "max": 130.0},
                "mode": "manual",
            },
        },
    ]


def test_v1_value_query_propagates_source_quality_and_timestamp(client: TestClient) -> None:
    async def read_data_values(node_ids: list[str]) -> list[Any]:
        return [
            SimpleNamespace(
                Value=SimpleNamespace(Value=99.5),
                StatusCode=SimpleNamespace(name="Uncertain", is_good=lambda: False),
                SourceTimestamp=datetime(2026, 3, 15, 8, 30, tzinfo=timezone.utc),
                ServerTimestamp=None,
            )
            for _ in node_ids
        ]

    fastapi_app(client).state.opcua_client.read_data_values = read_data_values

    response = client.post(
        "/v1/objects/value",
        json={"elementIds": ["property-abc"], "maxDepth": 1},
    )
    assert response.status_code == 200
    payload = response.json()
    result = payload["results"][0]["result"]
    assert result["quality"] == "Uncertain"
    assert result["timestamp"] == "2026-03-15T08:30:00Z"
    assert result["value"] == 99.5


def test_v1_value_query_bad_quality_null_value_is_allowed(client: TestClient) -> None:
    async def read_data_values(node_ids: list[str]) -> list[Any]:
        return [
            SimpleNamespace(
                Value=SimpleNamespace(Value=None),
                StatusCode=SimpleNamespace(name="Bad", is_good=lambda: False),
                SourceTimestamp=datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc),
                ServerTimestamp=None,
            )
            for _ in node_ids
        ]

    fastapi_app(client).state.opcua_client.read_data_values = read_data_values

    response = client.post(
        "/v1/objects/value",
        json={"elementIds": ["property-abc"], "maxDepth": 1},
    )
    assert response.status_code == 200
    payload = response.json()
    result = payload["results"][0]["result"]
    assert result["quality"] == "Bad"
    assert result["value"] is None


def test_v1_value_query_null_with_good_quality_normalized_to_goodnodata(client: TestClient) -> None:
    async def read_data_values(node_ids: list[str]) -> list[Any]:
        return [
            SimpleNamespace(
                Value=SimpleNamespace(Value=None),
                StatusCode=SimpleNamespace(name="Good", is_good=lambda: True),
                SourceTimestamp=datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc),
                ServerTimestamp=None,
            )
            for _ in node_ids
        ]

    fastapi_app(client).state.opcua_client.read_data_values = read_data_values

    response = client.post(
        "/v1/objects/value",
        json={"elementIds": ["property-abc"], "maxDepth": 1},
    )
    assert response.status_code == 200
    payload = response.json()
    result = payload["results"][0]["result"]
    assert result["value"] is None
    assert result["quality"] == "GoodNoData"
