from __future__ import annotations

import os
from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from i3x_server.main import create_app
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult


class FakeOpcUaClient:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {"ns=2;s=Temperature": 42.5}

    async def read_value(self, node_id: str) -> Any:
        return self.values[node_id]

    async def call_method(self, object_node_id: str, method_node_id: str, args: list[Any]) -> Any:
        return {"object": object_node_id, "method": method_node_id, "args": args}


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    os.environ["I3X_SKIP_OPCUA_CONNECT"] = "1"
    app = create_app()

    property_id = "property-abc"
    action_id = "action-def"
    root_id = "asset-root"

    with TestClient(app) as test_client:
        app.state.model_cache = BuildResult(
            nodes_by_id={
                root_id: ModelNode(
                    id=root_id,
                    name="Machine",
                    kind="asset",
                    type=None,
                    children=[property_id, action_id],
                    source_node_id="ns=2;s=Machine",
                ),
                property_id: ModelNode(
                    id=property_id,
                    name="Temperature",
                    kind="property",
                    type="Double",
                    children=[],
                    source_node_id="ns=2;s=Temperature",
                ),
                action_id: ModelNode(
                    id=action_id,
                    name="Reset",
                    kind="action",
                    type=None,
                    children=[],
                    source_node_id="ns=2;s=Reset",
                ),
            },
            root_ids=[root_id],
            children_by_id={root_id: [property_id, action_id], property_id: [], action_id: []},
            property_to_node={property_id: "ns=2;s=Temperature"},
            action_to_method={action_id: ("ns=2;s=Machine", "ns=2;s=Reset")},
        )
        app.state.opcua_client = FakeOpcUaClient()
        yield test_client


def test_get_model(client: TestClient) -> None:
    response = client.get("/model")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["items"]) == 1


def test_get_data_value(client: TestClient) -> None:
    response = client.get("/data/property-abc")
    assert response.status_code == 200
    payload = response.json()
    assert payload["property_id"] == "property-abc"
    assert payload["value"] == 42.5


def test_invoke_action(client: TestClient) -> None:
    response = client.post("/action/action-def/invoke", json={"args": [1, "x"]})
    assert response.status_code == 200
    payload = response.json()
    assert payload["action_id"] == "action-def"
    assert payload["result"]["args"] == [1, "x"]
