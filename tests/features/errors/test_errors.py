"""Error handling tests."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_v1_validation_error_includes_response_detail(client: TestClient) -> None:
    response = client.post(
        "/v1/objects/value",
        json={"maxDepth": 1},
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == 400
    assert payload["responseDetail"]["status"] == 400
    assert payload["responseDetail"]["title"] == "Bad Request"


def test_v1_404_error_includes_response_detail(client: TestClient) -> None:
    response = client.post(
        "/v1/subscriptions/sync",
        json={"clientId": "my-app-instance-001", "subscriptionId": "missing"},
    )
    assert response.status_code == 404
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == 404
    assert payload["responseDetail"]["status"] == 404
    assert payload["responseDetail"]["title"] == "Not Found"


def test_v1_501_error_includes_response_detail(client: TestClient) -> None:
    response = client.put("/v1/objects/some-id/value")
    assert response.status_code == 501
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == 501
    assert payload["responseDetail"]["status"] == 501
    assert payload["responseDetail"]["title"] == "Not Implemented"
