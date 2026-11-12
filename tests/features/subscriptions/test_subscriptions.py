"""Subscription lifecycle tests."""

from __future__ import annotations

import base64
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from i3x_server.api.v1.contracts import MAX_REQUEST_DEPTH
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.opcua_value import normalize_opcua_value
from tests.conftest import (
    _client_with_env,
    add_composition_depth_chain,
    assert_documented_response,
    fastapi_app,
)


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

    # Poll sync until the gateway surfaces updates instead of sleeping a fixed 1.2s: the
    # initial-value snapshot is queued at registration, so the result must become
    # non-empty within the polling budget.
    deadline = time.monotonic() + 2.5
    result: list[Any] = []
    while time.monotonic() < deadline:
        synced = client.post(
            "/v1/subscriptions/sync",
            json={"clientId": client_id, "subscriptionId": subscription_id, "acknowledgeSequence": 0},
        )
        assert synced.status_code == 200
        sync_payload = synced.json()
        assert sync_payload["success"] is True
        assert isinstance(sync_payload["result"], list)
        result = sync_payload["result"]
        if result:
            break
        time.sleep(0.05)

    assert result, "sync must surface subscription updates within the polling budget"
    assert result[0]["sequenceNumber"] >= 1
    assert isinstance(result[0]["updates"], list)
    assert result[0]["updates"][0]["elementId"]


def test_v1_subscription_client_id_is_one_normalized_scope(client: TestClient) -> None:
    """Surrounding whitespace must be trimmed at the boundary: create/register/sync/list/delete share one scope."""
    created = client.post(
        "/v1/subscriptions",
        json={"clientId": "  padded-client  ", "displayName": "Padded Monitor"},
    )
    assert created.status_code == 200
    created_payload = created.json()
    assert created_payload["result"]["clientId"] == "padded-client"
    subscription_id = created_payload["result"]["subscriptionId"]

    register = client.post(
        "/v1/subscriptions/register",
        json={
            "clientId": " padded-client ",
            "subscriptionId": subscription_id,
            "elementIds": ["property-abc"],
            "maxDepth": 1,
        },
    )
    assert register.status_code == 200

    synced = client.post(
        "/v1/subscriptions/sync",
        json={"clientId": "padded-client ", "subscriptionId": subscription_id, "acknowledgeSequence": 0},
    )
    assert synced.status_code == 200

    listed = client.post(
        "/v1/subscriptions/list",
        json={"clientId": "padded-client", "subscriptionIds": [subscription_id]},
    )
    assert listed.status_code == 200
    assert listed.json()["results"][0]["result"]["subscriptionId"] == subscription_id

    deleted = client.post(
        "/v1/subscriptions/delete",
        json={"clientId": " padded-client", "subscriptionIds": [subscription_id]},
    )
    assert deleted.status_code == 200
    assert deleted.json()["results"][0]["success"] is True

    after_delete = client.post(
        "/v1/subscriptions/list",
        json={"clientId": "padded-client", "subscriptionIds": [subscription_id]},
    )
    assert after_delete.json()["results"][0]["success"] is False


@pytest.mark.parametrize("client_id", ["", "   ", "\t", "client\nnewline"])
def test_v1_subscription_rejects_invalid_client_id(client: TestClient, client_id: str) -> None:
    """Empty, whitespace-only, and non-printable clientIds are rejected with the documented 400 envelope."""
    response = client.post(
        "/v1/subscriptions",
        json={"clientId": client_id, "displayName": "Invalid Monitor"},
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == 400
    assert payload["responseDetail"]["status"] == 400
    assert_documented_response(client, "post", "/v1/subscriptions", 400, payload)


def test_v1_subscription_register_defaults_to_one_composition_level(client: TestClient) -> None:
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
    # Relationship planes (i3X spec): asset-child is a hierarchical child (independent object),
    # property-def is a composition child of asset-child.
    model.hierarchy_children_by_id["asset-root"] = ["asset-child"]
    model.composition_children_by_id["asset-child"] = ["property-def"]
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
    assert "property-abc" in element_ids
    assert "property-def" not in element_ids

    details = client.post(
        "/v1/subscriptions/list",
        json={"clientId": client_id, "subscriptionIds": [subscription_id]},
    ).json()
    assert details["results"][0]["result"]["monitoredObjects"][0]["maxDepth"] == 1

    # Unbounded composition depth still never follows HasChildren (spec :1111/:1924).
    register_all = client.post(
        "/v1/subscriptions/register",
        json={
            "clientId": client_id,
            "subscriptionId": subscription_id,
            "elementIds": ["asset-root"],
            "maxDepth": 0,
        },
    )
    assert register_all.status_code == 200
    synced_all = client.post(
        "/v1/subscriptions/sync",
        json={"clientId": client_id, "subscriptionId": subscription_id},
    )
    assert synced_all.status_code == 200
    all_element_ids = {item["elementId"] for batch in synced_all.json()["result"] for item in batch["updates"]}
    assert "property-abc" in all_element_ids
    assert "property-def" not in all_element_ids

    # A hierarchical child is monitored by registering it on its own.
    register_child = client.post(
        "/v1/subscriptions/register",
        json={
            "clientId": client_id,
            "subscriptionId": subscription_id,
            "elementIds": ["asset-child"],
        },
    )
    assert register_child.status_code == 200
    synced_child = client.post(
        "/v1/subscriptions/sync",
        json={"clientId": client_id, "subscriptionId": subscription_id},
    )
    assert synced_child.status_code == 200
    child_element_ids = {item["elementId"] for batch in synced_child.json()["result"] for item in batch["updates"]}
    assert "property-def" in child_element_ids

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
    service._append_update(state, "ns=2;s=RawBytes", normalize_opcua_value(b"\xff\x00"))

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
    assert payload["result"][0]["updates"][0]["value"] == base64.b64encode(b"\xff\x00").decode("ascii")


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
    assert_documented_response(client, "post", "/v1/subscriptions/sync", 206, payload)


def test_v1_subscription_sync_last_sequence_number_minus_one_clears_pending_updates(client: TestClient) -> None:
    created = client.post(
        "/v1/subscriptions",
        json={"clientId": "my-app-instance-001", "displayName": "Ack All Monitor"},
    )
    assert created.status_code == 200
    subscription_id = created.json()["result"]["subscriptionId"]

    service = fastapi_app(client).state.subscription_service
    state = service._subscriptions[subscription_id]
    state.node_to_element_id["ns=2;s=Temperature"] = "property-abc"

    service._append_update(state, "ns=2;s=Temperature", 10.0)
    service._append_update(state, "ns=2;s=Temperature", 11.0)

    first = client.post(
        "/v1/subscriptions/sync",
        json={
            "clientId": "my-app-instance-001",
            "subscriptionId": subscription_id,
            "acknowledgeSequence": 0,
        },
    )
    assert first.status_code == 200
    first_payload = first.json()
    assert [item["sequenceNumber"] for item in first_payload["result"][0]["updates"]] == [1, 2]

    ack_all = client.post(
        "/v1/subscriptions/sync",
        json={
            "clientId": "my-app-instance-001",
            "subscriptionId": subscription_id,
            "lastSequenceNumber": -1,
        },
    )
    assert ack_all.status_code == 200
    ack_all_payload = ack_all.json()
    assert ack_all_payload["success"] is True
    assert ack_all_payload["result"] == []

    post_ack = client.post(
        "/v1/subscriptions/sync",
        json={
            "clientId": "my-app-instance-001",
            "subscriptionId": subscription_id,
            "acknowledgeSequence": 0,
        },
    )
    assert post_ack.status_code == 200
    assert post_ack.json()["result"] == []


def test_v1_subscription_stream_not_found(client: TestClient) -> None:
    response = client.post(
        "/v1/subscriptions/stream",
        json={"clientId": "my-app-instance-001", "subscriptionId": "missing"},
    )
    assert response.status_code == 404


def test_v1_subscription_stream_rejects_acknowledgement_fields(client: TestClient) -> None:
    response_ack = client.post(
        "/v1/subscriptions/stream",
        json={"clientId": "my-app-instance-001", "subscriptionId": "missing", "acknowledgeSequence": 4},
    )
    assert response_ack.status_code == 400

    response_legacy = client.post(
        "/v1/subscriptions/stream",
        json={"clientId": "my-app-instance-001", "subscriptionId": "missing", "lastSequenceNumber": 4},
    )
    assert response_legacy.status_code == 400


def test_v1_subscription_count_cap_answers_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process-wide subscription cap answers 429 with the error envelope; a delete frees the slot."""
    with _client_with_env(monkeypatch, {"I3X_SUBSCRIPTION_MAX_COUNT": "1"}) as client:
        created = client.post("/v1/subscriptions", json={"clientId": "capped-client", "displayName": "First"})
        assert created.status_code == 200
        subscription_id = created.json()["result"]["subscriptionId"]

        overflow = client.post("/v1/subscriptions", json={"clientId": "other-client", "displayName": "Second"})
        assert overflow.status_code == 429
        payload = overflow.json()
        assert payload["success"] is False
        assert payload["error"] == {"code": 429, "message": "Subscription limit reached"}
        assert payload["responseDetail"]["status"] == 429
        assert payload["responseDetail"]["title"] == "Too Many Requests"
        assert "maxSubscriptions=1" in payload["responseDetail"]["detail"]
        assert "activeSubscriptions=1" in payload["responseDetail"]["detail"]
        assert_documented_response(client, "post", "/v1/subscriptions", 429, payload)

        # A rejected create must leave no state behind, or it would occupy the slot it just exceeded.
        service = fastapi_app(client).state.subscription_service
        assert list(service._subscriptions) == [subscription_id]

        deleted = client.post(
            "/v1/subscriptions/delete",
            json={"clientId": "capped-client", "subscriptionIds": [subscription_id]},
        )
        assert deleted.status_code == 200
        assert deleted.json()["results"][0]["success"] is True

        refilled = client.post("/v1/subscriptions", json={"clientId": "other-client", "displayName": "Second"})
        assert refilled.status_code == 200


def test_v1_register_rejects_oversized_subtree_with_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """An oversized subtree is rejected as 400 and never truncated; the monitored set stays intact."""
    with _client_with_env(monkeypatch, {"I3X_SUBSCRIPTION_SUBTREE_MAX_NODES": "2"}) as client:
        app = fastapi_app(client)
        add_composition_depth_chain(app)
        created = client.post("/v1/subscriptions", json={"clientId": "budget-client"})
        assert created.status_code == 200
        subscription_id = created.json()["result"]["subscriptionId"]

        first = client.post(
            "/v1/subscriptions/register",
            json={
                "clientId": "budget-client",
                "subscriptionId": subscription_id,
                "elementIds": ["property-abc"],
                "maxDepth": 1,
            },
        )
        assert first.status_code == 200
        state = app.state.subscription_service._subscriptions[subscription_id]
        assert state.monitored_objects == {"property-abc": 1}
        assert state.monitored_node_ids == {"ns=2;s=Temperature"}

        # ``asset-root`` at depth 0 collects three composition properties (comp, mid, deep): over budget.
        rejected = client.post(
            "/v1/subscriptions/register",
            json={
                "clientId": "budget-client",
                "subscriptionId": subscription_id,
                "elementIds": ["asset-root"],
                "maxDepth": 0,
            },
        )
        assert rejected.status_code == 400
        payload = rejected.json()
        assert payload["success"] is False
        assert payload["error"] == {"code": 400, "message": "Monitored item budget exceeded"}
        assert payload["responseDetail"]["title"] == "Bad Request"
        assert "elementId=asset-root" in payload["responseDetail"]["detail"]
        assert "monitoredNodes=3" in payload["responseDetail"]["detail"]
        assert "maxNodes=2" in payload["responseDetail"]["detail"]
        assert_documented_response(client, "post", "/v1/subscriptions/register", 400, payload)

        # Rejection is atomic: no truncation to the cap, no loss of what was already monitored.
        assert state.monitored_objects == {"property-abc": 1}
        assert state.monitored_node_ids == {"ns=2;s=Temperature"}


def test_v1_max_depth_at_gateway_bound_is_accepted(client: TestClient) -> None:
    """The admission bound is inclusive: ``maxDepth == MAX_REQUEST_DEPTH`` is a normal request."""
    client_id = "depth-bound-client"
    created = client.post("/v1/subscriptions", json={"clientId": client_id})
    assert created.status_code == 200
    subscription_id = created.json()["result"]["subscriptionId"]

    register = client.post(
        "/v1/subscriptions/register",
        json={
            "clientId": client_id,
            "subscriptionId": subscription_id,
            "elementIds": ["asset-root"],
            "maxDepth": MAX_REQUEST_DEPTH,
        },
    )
    assert register.status_code == 200

    value = client.post("/v1/objects/value", json={"elementIds": ["asset-root"], "maxDepth": MAX_REQUEST_DEPTH})
    assert value.status_code == 200


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "/v1/subscriptions/register",
            {
                "clientId": "depth-bound-client",
                "subscriptionId": "does-not-exist",
                "elementIds": ["asset-root"],
                "maxDepth": MAX_REQUEST_DEPTH + 1,
            },
        ),
        ("/v1/objects/value", {"elementIds": ["asset-root"], "maxDepth": MAX_REQUEST_DEPTH + 1}),
        (
            "/v1/objects/history",
            {
                "elementIds": ["asset-root"],
                "startTime": "2026-01-01T00:00:00Z",
                "endTime": "2026-01-02T00:00:00Z",
                "maxDepth": MAX_REQUEST_DEPTH + 1,
            },
        ),
    ],
)
def test_v1_max_depth_above_gateway_bound_is_rejected_with_400(
    client: TestClient, path: str, body: dict[str, Any]
) -> None:
    """An over-bound ``maxDepth`` is rejected before any traversal or upstream lookup happens."""
    response = client.post(path, json=body)
    assert response.status_code == 400
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == 400
    assert payload["responseDetail"]["status"] == 400
    assert "maxDepth" in payload["responseDetail"]["detail"]
    assert_documented_response(client, "post", path, 400, payload)
