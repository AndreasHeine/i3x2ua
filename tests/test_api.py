from __future__ import annotations

import os
import time
from collections.abc import Generator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from i3x_server.main import create_app
from i3x_server.opcua.client import OpcUaSubscriptionCapabilities
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult
from i3x_server.subscriptions.service import SubscriptionService


class FakeOpcUaClient:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {"ns=2;s=Temperature": 42.5}
        self._reads = 0
        self._listeners: list[Any] = []

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

    async def read_values(self, node_ids: list[str]) -> list[Any]:
        self._reads += 1
        results: list[Any] = []
        for node_id in node_ids:
            base = self.values.get(node_id, 1.0)
            value = float(base) + self._reads
            self.values[node_id] = value
            results.append(value)
        return results

    async def get_subscription_capabilities(self) -> OpcUaSubscriptionCapabilities:
        return OpcUaSubscriptionCapabilities(
            max_monitored_items_per_call=1,
            max_subscriptions=100,
            max_monitored_items=100,
            max_subscriptions_per_session=100,
            max_monitored_items_per_subscription=1,
        )

    async def create_datachange_subscription(self, publishing_interval_ms: float, handler: Any) -> Any:
        return SimpleNamespace(delete=self._noop_async)

    async def subscribe_data_changes(self, subscription: Any, node_ids: list[str]) -> list[int]:
        return list(range(len(node_ids)))

    async def delete_subscription(self, subscription: Any) -> None:
        await self._noop_async()

    def add_reconnect_listener(self, listener: Any) -> None:
        self._listeners.append(listener)

    async def call_method(self, object_node_id: str, method_node_id: str, args: list[Any]) -> Any:
        return {"object": object_node_id, "method": method_node_id, "args": args}

    async def _noop_async(self) -> None:
        return None


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
        app.state.subscription_service = SubscriptionService(app.state.opcua_client, interval_seconds=1)
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
    assert payload["result"]["capabilities"]["subscribe"]["stream"] is True


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


def test_beta_subscription_lifecycle(client: TestClient) -> None:
    created = client.post(
        "/v1/subscriptions",
        json={"clientId": "my-app-instance-001", "displayName": "Dashboard Monitor"},
    )
    assert created.status_code == 200
    created_payload = created.json()
    subscription_id = created_payload["result"]["subscriptionId"]

    register = client.post(
        "/v1/subscriptions/register",
        json={
            "subscriptionId": subscription_id,
            "elementIds": ["property-abc", "ns=2;s=OtherTemp"],
            "maxDepth": 1,
        },
    )
    assert register.status_code == 200

    listed = client.post("/v1/subscriptions/list", json={"subscriptionIds": [subscription_id]})
    assert listed.status_code == 200
    list_payload = listed.json()
    assert list_payload["result"][0]["subscriptionId"] == subscription_id
    assert list_payload["result"][0]["mode"] == "polling"

    time.sleep(1.2)

    synced = client.post(
        "/v1/subscriptions/sync",
        json={"subscriptionId": subscription_id, "acknowledgeSequence": 0},
    )
    assert synced.status_code == 200
    sync_payload = synced.json()
    assert sync_payload["success"] is True
    assert len(sync_payload["result"]) >= 1
    assert sync_payload["result"][0]["elementId"]
    assert sync_payload["result"][0]["nodeId"]

    deleted = client.post("/v1/subscriptions/delete", json={"subscriptionIds": [subscription_id]})
    assert deleted.status_code == 200
    deleted_payload = deleted.json()
    assert deleted_payload["results"][0]["success"] is True


def test_beta_subscription_stream_not_found(client: TestClient) -> None:
    response = client.post("/v1/subscriptions/stream", json={"subscriptionId": "missing"})
    assert response.status_code == 404


def test_beta_subscription_stream_not_found_with_ack_fields(client: TestClient) -> None:
    response_ack = client.post(
        "/v1/subscriptions/stream",
        json={"subscriptionId": "missing", "acknowledgeSequence": 4},
    )
    assert response_ack.status_code == 404

    response_legacy = client.post(
        "/v1/subscriptions/stream",
        json={"subscriptionId": "missing", "lastSequenceNumber": 4},
    )
    assert response_legacy.status_code == 404
