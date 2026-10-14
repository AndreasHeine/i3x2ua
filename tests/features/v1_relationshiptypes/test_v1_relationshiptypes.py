"""RelationshipTypes endpoints tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import fastapi_app


def test_v1_relationshiptypes(client: TestClient) -> None:
    response = client.get("/v1/relationshiptypes")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert len(payload["result"]) >= 4
    ids = {item["elementId"] for item in payload["result"]}
    assert "HasComponent" in ids
    assert "HasParent" in ids


def test_v1_relationshiptypes_query(client: TestClient) -> None:
    response = client.post(
        "/v1/relationshiptypes/query",
        json={"elementIds": ["HasComponent", "missing-relationship"]},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["results"][0]["success"] is True
    assert payload["results"][0]["result"]["relationshipId"] == "HasComponent"
    assert payload["results"][1]["success"] is False
    assert payload["results"][1]["error"]["code"] == 404


@pytest.mark.parametrize("namespace_uri", ["http://opcfoundation.org/UA/", "urn:example:custom"])
@pytest.mark.parametrize("qualified", [False, True])
def test_relationship_namespace_filter_and_id_query(client: TestClient, namespace_uri: str, qualified: bool) -> None:
    relationship_id = f"{namespace_uri}:ConnectedTo" if qualified else "ConnectedTo"
    reverse_id = f"inverseOf_{relationship_id}"
    model = fastapi_app(client).state.model_cache
    model.graph_relationship_names = {relationship_id}
    model.graph_relationship_namespace_by_id = {relationship_id: namespace_uri}
    model.graph_relationship_display_name_by_id = {relationship_id: "ConnectedTo"}
    model.relationships_by_id = {
        "asset-root": {relationship_id: ["sensor-root"]},
        "sensor-root": {reverse_id: ["asset-root"]},
    }
    model.revision = ""

    response = client.get("/v1/relationshiptypes", params={"namespaceUri": namespace_uri})
    assert response.status_code == 200
    items = response.json()["result"]
    assert {item["elementId"] for item in items} == {relationship_id, reverse_id}
    assert all(item["namespaceUri"] == namespace_uri for item in items)
    assert [item["displayName"] for item in items] == ["ConnectedTo", "inverseOf_ConnectedTo"]
    query = client.post("/v1/relationshiptypes/query", json={"elementIds": [relationship_id, reverse_id]})
    assert query.status_code == 200
    assert query.json()["success"] is True
    assert [item["result"] for item in query.json()["results"]] == items
    for source_id, target_id, edge in [
        ("asset-root", "sensor-root", relationship_id),
        ("sensor-root", "asset-root", reverse_id),
    ]:
        related = client.post("/v1/objects/related", json={"elementIds": [source_id], "relationshipType": edge})
        assert related.status_code == 200
        assert related.json()["success"] is True
        relations = related.json()["results"][0]["result"]
        assert len(relations) == 1
        assert relations[0]["sourceRelationship"] == edge
        assert relations[0]["object"]["elementId"] == target_id
    assert (
        client.get("/v1/relationshiptypes", params={"namespaceUri": "https://opcfoundation.org/UA/"}).json()["result"]
        == []
    )


def test_relationship_namespace_change_invalidates_etag(client: TestClient) -> None:
    model = fastapi_app(client).state.model_cache
    model.graph_relationship_names = {"ConnectedTo"}
    model.graph_relationship_namespace_by_id = {"ConnectedTo": "urn:example:first"}
    model.revision = ""
    first = client.get("/v1/relationshiptypes", params={"namespaceUri": "urn:example:first"})
    assert len(first.json()["result"]) == 2

    model.graph_relationship_namespace_by_id = {"ConnectedTo": "urn:example:second"}
    model.revision = ""
    second = client.get(
        "/v1/relationshiptypes",
        params={"namespaceUri": "urn:example:first"},
        headers={"If-None-Match": first.headers["etag"]},
    )
    assert second.status_code == 200
    assert second.json()["result"] == []
    assert second.headers["etag"] != first.headers["etag"]
