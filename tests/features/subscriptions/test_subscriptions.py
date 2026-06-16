"""Subscription lifecycle tests."""

from __future__ import annotations

import base64
import time

from fastapi.testclient import TestClient

from i3x_server.schemas.i3x import ModelNode
from tests.conftest import fastapi_app


def test_v1_subscription_lifecycle(client: TestClient) -> None:
    client_id = "my-app-instance-001"
    created = client.post(
        "/v1/subscriptions",
        json={"clientId": client_id, "displayName": "Dashboard Monitor"},
    )
    assert created.status_code == 200
    created_payload = created.json()
    subscription_id = created_payload["result"]["subscriptionId"]

    register = client.post(
        "/v1/subscriptions/register",
        json={
            "clientId": client_id,
            "subscriptionId": subscription_id,
            "elementIds": ["property-abc", "ns=2;s=OtherTemp"],
            "maxDepth": 1,
        },
    )
    assert register.status_code == 200
    register_payload = register.json()
    assert register_payload["success"] is False

    listed = client.post(
        "/v1/subscriptions/list",
        json={"clientId": client_id, "subscriptionIds": [subscription_id]},
    )
    assert listed.status_code == 200
    list_payload = listed.json()
    assert list_payload["results"][0]["result"]["subscriptionId"] == subscription_id
    assert list_payload["results"][0]["result"]["mode"] in {"polling", "native"}

    time.sleep(1.2)

    synced = client.post(
        "/v1/subscriptions/sync",
        json={"clientId": client_id, "subscriptionId": subscription_id, "acknowledgeSequence": 0},
    )
    assert synced.status_code == 200
    sync_payload = synced.json()
    assert sync_payload["success"] is True
    assert isinstance(sync_payload["result"], list)
    if sync_payload["result"]:
        assert sync_payload["result"][0]["sequenceNumber"] >= 1
        assert isinstance(sync_payload["result"][0]["updates"], list)
        assert sync_payload["result"][0]["updates"][0]["elementId"]


def test_v1_subscription_register_omitted_maxdepth_monitors_descendant_properties(client: TestClient) -> None:
    client_id = "my-app-instance-001"
    app = fastapi_app(client)
    model = app.state.model_cache
    model.nodes_by_id["asset-child"] = ModelNode(
        id="asset-child",
        name="Nested Asset",
        kind="asset",
        type=None,
        children=["property-def"],
        source_node_id="ns=2;s=NestedAsset",
        source_type_id="ns=1;i=1003",
    )
    model.nodes_by_id["property-def"] = ModelNode(
        id="property-def",
        name="Pressure",
        kind="property",
        type="ns=1;i=11",
        children=[],
        source_node_id="ns=2;s=Pressure",
    )
    model.children_by_id["asset-child"] = ["property-def"]
    model.children_by_id["property-def"] = []
    model.children_by_id["asset-root"] = ["property-abc", "action-def", "asset-child"]
    model.property_to_node["property-def"] = "ns=2;s=Pressure"
    app.state.opcua_client.values["ns=2;s=Pressure"] = 17.5

    created = client.post(
        "/v1/subscriptions",
        json={"clientId": client_id, "displayName": "Deep Monitor"},
    )
    assert created.status_code == 200
    subscription_id = created.json()["result"]["subscriptionId"]

    register = client.post(
        "/v1/subscriptions/register",
        json={
            "clientId": client_id,
            "subscriptionId": subscription_id,
            "elementIds": ["asset-root"],
        },
    )
    assert register.status_code == 200

    synced = client.post(
        "/v1/subscriptions/sync",
        json={"clientId": client_id, "subscriptionId": subscription_id},
    )
    assert synced.status_code == 200
    payload = synced.json()
    assert payload["success"] is True
    assert len(payload["result"]) == 1
    updates = payload["result"][0]["updates"]
    element_ids = {item["elementId"] for item in updates}
    assert {"property-abc", "property-def"}.issubset(element_ids)

    deleted = client.post(
        "/v1/subscriptions/delete",
        json={"clientId": client_id, "subscriptionIds": [subscription_id]},
    )
    assert deleted.status_code == 200
    deleted_payload = deleted.json()
    assert deleted_payload["results"][0]["success"] is True


def test_v1_subscription_sync_serializes_binary_values(client: TestClient) -> None:
    created = client.post(
        "/v1/subscriptions",
        json={"clientId": "my-app-instance-001", "displayName": "Binary Monitor"},
    )
    assert created.status_code == 200
    subscription_id = created.json()["result"]["subscriptionId"]

    service = fastapi_app(client).state.subscription_service
    state = service._subscriptions[subscription_id]
    service._append_update(state, "ns=2;s=RawBytes", b"\xff\x00")

    synced = client.post(
        "/v1/subscriptions/sync",
        json={
            "clientId": "my-app-instance-001",
            "subscriptionId": subscription_id,
            "acknowledgeSequence": 0,
        },
    )
    assert synced.status_code == 200
    payload = synced.json()
    assert payload["success"] is True
    assert payload["result"][0]["updates"][0]["value"] == {
        "encoding": "base64",
        "data": base64.b64encode(b"\xff\x00").decode("ascii"),
    }


def test_v1_subscription_register_accepts_source_node_id(client: TestClient) -> None:
    client_id = "my-app-instance-001"
    created = client.post(
        "/v1/subscriptions",
        json={"clientId": client_id, "displayName": "Source Node Monitor"},
    )
    assert created.status_code == 200
    subscription_id = created.json()["result"]["subscriptionId"]

    register = client.post(
        "/v1/subscriptions/register",
        json={
            "clientId": client_id,
            "subscriptionId": subscription_id,
            "elementIds": ["ns=2;s=Temperature"],
        },
    )
    assert register.status_code == 200
    payload = register.json()
    assert payload["success"] is True
    assert payload["results"][0]["success"] is True


def test_v1_subscription_sync_null_value_uses_goodnodata(client: TestClient) -> None:
    created = client.post(
        "/v1/subscriptions",
        json={"clientId": "my-app-instance-001", "displayName": "Null Monitor"},
    )
    assert created.status_code == 200
    subscription_id = created.json()["result"]["subscriptionId"]

    service = fastapi_app(client).state.subscription_service
    state = service._subscriptions[subscription_id]
    state.node_to_element_id["ns=2;s=NullValue"] = "ns=2;s=NullValue"

    service._append_update(state, "ns=2;s=NullValue", None)

    synced = client.post(
        "/v1/subscriptions/sync",
        json={
            "clientId": "my-app-instance-001",
            "subscriptionId": subscription_id,
            "acknowledgeSequence": 0,
        },
    )
    assert synced.status_code == 200
    payload = synced.json()
    assert payload["success"] is True
    assert payload["result"][0]["updates"][0]["value"] is None
    assert payload["result"][0]["updates"][0]["quality"] == "GoodNoData"


def test_v1_subscription_sync_rejects_when_stream_active(client: TestClient) -> None:
    created = client.post(
        "/v1/subscriptions",
        json={"clientId": "my-app-instance-001", "displayName": "Stream Lock"},
    )
    assert created.status_code == 200
    subscription_id = created.json()["result"]["subscriptionId"]

    service = fastapi_app(client).state.subscription_service
    state = service._subscriptions[subscription_id]
    state.stream_connected = True
    state.active_stream_generation = 1

    synced = client.post(
        "/v1/subscriptions/sync",
        json={
            "clientId": "my-app-instance-001",
            "subscriptionId": subscription_id,
            "acknowledgeSequence": 0,
        },
    )
    assert synced.status_code == 400
    payload = synced.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == 400

    state.stream_connected = False


def test_v1_subscription_sync_returns_206_on_overflow(client: TestClient) -> None:
    created = client.post(
        "/v1/subscriptions",
        json={"clientId": "my-app-instance-001", "displayName": "Overflow Monitor"},
    )
    assert created.status_code == 200
    subscription_id = created.json()["result"]["subscriptionId"]

    service = fastapi_app(client).state.subscription_service
    service._max_updates_per_subscription = 2
    state = service._subscriptions[subscription_id]
    state.node_to_element_id["ns=2;s=Overflow1"] = "ns=2;s=Overflow1"
    state.node_to_element_id["ns=2;s=Overflow2"] = "ns=2;s=Overflow2"
    state.node_to_element_id["ns=2;s=Overflow3"] = "ns=2;s=Overflow3"

    service._append_update(state, "ns=2;s=Overflow1", 1)
    service._append_update(state, "ns=2;s=Overflow2", 2)
    service._append_update(state, "ns=2;s=Overflow3", 3)

    synced = client.post(
        "/v1/subscriptions/sync",
        json={
            "clientId": "my-app-instance-001",
            "subscriptionId": subscription_id,
            "acknowledgeSequence": 0,
        },
    )
    assert synced.status_code == 206
    payload = synced.json()
    assert payload["success"] is True
    assert isinstance(payload["result"], list)
    assert payload["responseDetail"]["status"] == 206
    assert "Dropped sequence numbers" in payload["responseDetail"]["detail"]


def test_v1_subscription_stream_not_found(client: TestClient) -> None:
    response = client.post(
        "/v1/subscriptions/stream",
        json={"clientId": "my-app-instance-001", "subscriptionId": "missing"},
    )
    assert response.status_code == 404


def test_v1_subscription_stream_not_found_with_ack_fields(client: TestClient) -> None:
    response_ack = client.post(
        "/v1/subscriptions/stream",
        json={"clientId": "my-app-instance-001", "subscriptionId": "missing", "acknowledgeSequence": 4},
    )
    assert response_ack.status_code == 404

    response_legacy = client.post(
        "/v1/subscriptions/stream",
        json={"clientId": "my-app-instance-001", "subscriptionId": "missing", "lastSequenceNumber": 4},
    )
    assert response_legacy.status_code == 404
