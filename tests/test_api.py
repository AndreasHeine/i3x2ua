from __future__ import annotations

import os
from collections.abc import Generator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from i3x_server.main import create_app
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult


class FakeOpcUaClient:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {"ns=2;s=Temperature": 42.5}

    async def get_namespace_infos(self) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(uri="http://example.com/i3x", display_name="I3X"),
            SimpleNamespace(uri="http://example.com/custom", display_name="Custom"),
        ]

    async def get_object_types(self) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                node_id="ns=1;i=1001",
                parent_node_id=None,
                browse_name="MachineType",
                display_name="Machine Type",
            ),
            SimpleNamespace(
                node_id="ns=1;i=1002",
                parent_node_id="ns=1;i=1001",
                browse_name="SensorType",
                display_name="Sensor Type",
            ),
        ]

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
    assert response.status_code == 404


def test_get_data_value(client: TestClient) -> None:
    response = client.get("/data/property-abc")
    assert response.status_code == 404


def test_invoke_action(client: TestClient) -> None:
    response = client.post("/action/action-def/invoke", json={"args": [1, "x"]})
    assert response.status_code == 404


def test_beta_info(client: TestClient) -> None:
    response = client.get("/v1/info")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["result"]["specVersion"] == "beta"


def test_beta_namespaces(client: TestClient) -> None:
    response = client.get("/v1/namespaces")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert len(payload["result"]) == 2


def test_beta_objecttypes(client: TestClient) -> None:
    response = client.get("/v1/objecttypes")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert len(payload["result"]) == 2


def test_beta_objects_list(client: TestClient) -> None:
    response = client.post("/v1/objects/list", json={"elementIds": ["asset-root", "missing"]})
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["results"][0]["success"] is True
    assert payload["results"][1]["success"] is False
