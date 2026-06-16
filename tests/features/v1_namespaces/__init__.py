"""Namespaces endpoint tests."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_v1_namespaces(client: TestClient) -> None:
    response = client.get("/v1/namespaces")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert len(payload["result"]) == 3
