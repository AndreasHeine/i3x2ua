"""Value query endpoints tests."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from i3x_server.schemas.i3x import ModelNode
from tests.conftest import FakeExtensionObject, FakeMachineConfig, FakeMachineThresholds, fastapi_app


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
    assert payload["results"][0]["result"]["value"] == {
        "encoding": "base64",
        "data": base64.b64encode(b"\xff\x00").decode("ascii"),
    }


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
