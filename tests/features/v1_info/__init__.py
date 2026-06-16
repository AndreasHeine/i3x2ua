"""V1 Info endpoint tests."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_v1_info(client: TestClient) -> None:
    response = client.get("/v1/info")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["result"]["specVersion"] == "1.0"
    assert payload["result"]["capabilities"]["query"]["history"] is True
    assert payload["result"]["capabilities"]["subscribe"]["stream"] is True
