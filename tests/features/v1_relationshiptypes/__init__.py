"""RelationshipTypes endpoints tests."""

from __future__ import annotations

from fastapi.testclient import TestClient


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
