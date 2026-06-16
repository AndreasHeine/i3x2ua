"""OPC UA diagnostics tests."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import fastapi_app


def test_ua_state(client: TestClient) -> None:
    response = client.get("/ua/state")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    result = payload["result"]
    assert "nodeId" not in result
    assert "hasTypeDefinition" not in result
    assert "quality" not in result
    assert "timestamp" not in result
    assert result["State"] == "Running"
    assert result["BuildInfo"]["ProductName"] == "UA Server"


def test_ua_connection(client: TestClient) -> None:
    response = client.get("/ua/connection")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    result = payload["result"]
    assert result["state"] == "Connected"
    assert result["endpoint"] == "opc.tcp://opcua.umati.app:4843"
    assert result["since"].endswith("Z")


def test_ua_limits(client: TestClient) -> None:
    response = client.get("/ua/limits")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    result = payload["result"]
    assert result["operationalLimits"]["maxNodesPerBrowse"] == 1000
    assert result["operationalLimits"]["maxNodesPerRead"] == 1000
    assert result["serverCapabilities"]["maxMonitoredItemsPerCall"] == 1
    assert result["serverCapabilities"]["maxSubscriptions"] == 100


def test_ua_metrics(client: TestClient) -> None:
    response = client.get("/ua/metrics")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    result = payload["result"]
    assert result["goodishQualities"] == ["Good", "Uncertain"]
    assert result["readCount"] == 2451
    assert result["writeCount"] == 0
    assert result["browseCount"] == 189
    assert result["methodCallCount"] == 27
    assert result["historyReadCount"] == 62
    assert result["historyWriteCount"] == 0
    assert result["failedRequestCount"] == 14


def test_ua_state_error_shape(client: TestClient) -> None:
    app = fastapi_app(client)

    async def _raise_status_error() -> Any:
        raise RuntimeError("boom")

    app.state.opcua_client.read_server_status_data_value = _raise_status_error
    response = client.get("/ua/state")
    assert response.status_code == 502
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == 502
    assert payload["error"]["message"] == "Failed to read OPC UA ServerStatus"
    assert payload["responseDetail"]["status"] == 502
    assert payload["responseDetail"]["detail"] == "Failed to read OPC UA ServerStatus"
