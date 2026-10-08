from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from asyncua import ua
from asyncua.ua.status_codes import StatusCodes
from asyncua.ua.uaerrors import UaStatusCodeError
from fastapi.testclient import TestClient

from tests.conftest import fastapi_app


def _update(element_id: str = "property-abc", value: Any = 19.5) -> dict[str, Any]:
    return {
        "elementId": element_id,
        "value": {"value": value, "quality": "Good", "timestamp": "2026-01-01T10:00:00Z"},
    }


def test_history_upsert_preserves_current_value(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")
    opcua = fastapi_app(client).state.opcua_client
    current = dict(opcua.values)
    records = opcua.history_values["ns=2;s=Temperature"]
    count = len(records)
    update = _update()
    update["value"]["quality"] = "Uncertain"
    update["value"]["timestamp"] = "2026-01-01T10:00:00Z"
    response = client.put("/v1/objects/history", json={"updates": [update]})
    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "results": [
            {
                "success": True,
                "elementId": "property-abc",
                "subscriptionId": None,
                "result": None,
                "error": None,
                "responseDetail": None,
            }
        ],
    }
    assert len(records) == count
    assert records[0].Value.Value == 19.5
    assert records[0].StatusCode.name == "Uncertain"
    assert records[0].SourceTimestamp == datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
    update["value"]["timestamp"] = "2026-01-01T10:02:00Z"
    response = client.put("/v1/objects/history", json={"updates": [update]})
    assert response.json()["success"] is True
    assert len(records) == count + 1
    assert opcua.values == current
    assert opcua.snapshot_request_metrics().history_write_count == 2
    assert opcua.snapshot_request_metrics().write_count == 0


def test_history_bulk_order_and_item_errors(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")
    updates = [_update(), _update("missing"), _update("asset-root"), _update(value="bad"), _update(value=20)]
    response = client.put("/v1/objects/history", json={"updates": updates})
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert [item["elementId"] for item in payload["results"]] == [item["elementId"] for item in updates]
    assert [item["success"] for item in payload["results"]] == [True, False, False, False, True]
    assert [item["responseDetail"]["status"] for item in payload["results"][1:4]] == [404, 400, 400]


@pytest.mark.parametrize("user_access", [False, True])
def test_history_access_is_independent_of_current_access(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    user_access: bool,
) -> None:
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")
    opcua = fastapi_app(client).state.opcua_client
    opcua.writable_by_node_id["ns=2;s=Temperature"] = False
    opcua.user_writable_by_node_id["ns=2;s=Temperature"] = False
    opcua.history_user_writable_by_node_id["ns=2;s=Temperature"] = user_access
    response = client.put("/v1/objects/history", json={"updates": [_update()]})
    assert response.json()["success"] is user_access
    if not user_access:
        assert response.json()["results"][0]["responseDetail"]["status"] == 403
    opcua.history_writable_by_node_id["ns=2;s=Temperature"] = False
    response = client.put("/v1/objects/history", json={"updates": [_update()]})
    assert response.json()["results"][0]["responseDetail"]["status"] == 403


@pytest.mark.parametrize(
    ("failure_code", "status"),
    [
        (StatusCodes.BadHistoryOperationUnsupported, 501),
        (StatusCodes.BadNotWritable, 403),
        (StatusCodes.BadUserAccessDenied, 403),
        (StatusCodes.BadNodeIdUnknown, 404),
        (StatusCodes.BadTypeMismatch, 400),
        (StatusCodes.BadHistoryOperationInvalid, 400),
        (StatusCodes.BadInvalidArgument, 400),
        (StatusCodes.BadTimestampNotSupported, 400),
        (None, 502),
    ],
)
def test_history_server_errors(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    failure_code: int | None,
    status: int,
) -> None:
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")
    failure: Exception = TimeoutError("timed out")
    if failure_code is not None:
        with pytest.raises(UaStatusCodeError) as error:
            ua.StatusCode(ua.UInt32(failure_code)).check()
        failure = error.value
    fastapi_app(client).state.opcua_client.history_write_failures["ns=2;s=Temperature"] = failure
    response = client.put("/v1/objects/history", json={"updates": [_update()]})
    assert response.status_code == 200
    assert response.json()["success"] is False
    assert response.json()["results"][0]["responseDetail"]["status"] == status


@pytest.mark.parametrize("field", ["value", "quality", "timestamp"])
def test_history_requires_full_vqt(client: TestClient, field: str) -> None:
    update = _update()
    del update["value"][field]
    assert client.put("/v1/objects/history", json={"updates": [update]}).status_code == 400


@pytest.mark.parametrize("timestamp", ["not-a-date", "2026-01-01T10:00:00", 123, "2026-01-01"])
def test_history_rejects_invalid_timestamp(client: TestClient, timestamp: Any) -> None:
    update = _update()
    update["value"]["timestamp"] = timestamp
    assert client.put("/v1/objects/history", json={"updates": [update]}).status_code == 400


@pytest.mark.parametrize(
    ("value", "quality", "valid"),
    [
        (None, "Good", False),
        (None, "Uncertain", False),
        (1, "GoodNoData", False),
        (1, "Bad", False),
        (1, "Unknown", False),
        (None, "Bad", True),
        (None, "GoodNoData", True),
    ],
)
def test_history_quality_validation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    value: Any,
    quality: str,
    valid: bool,
) -> None:
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")
    fastapi_app(client).state.model_cache.nodes_by_id["property-abc"].type = "nsu=http://opcfoundation.org/UA/;i=11"
    update = _update(value=value)
    update["value"]["quality"] = quality
    response = client.put("/v1/objects/history", json={"updates": [update]})
    assert response.status_code == (200 if valid else 400)
    if valid:
        history = client.post(
            "/v1/objects/history",
            json={
                "elementIds": ["property-abc"],
                "startTime": "2026-01-01T00:00:00Z",
                "endTime": "2026-01-02T00:00:00Z",
            },
        )
        record = history.json()["results"][0]["result"]["values"][0]
        assert record == {"value": value, "quality": quality, "timestamp": "2026-01-01T10:00:00Z"}


def test_history_write_gate_and_capability(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    for enabled in ("0", "1"):
        monkeypatch.setenv("I3X_ENABLE_WRITES", enabled)
        capabilities = client.get("/v1/info").json()["result"]["capabilities"]["update"]
        assert capabilities == {"current": enabled == "1", "history": enabled == "1"}
        response = client.put("/v1/objects/history", json={"updates": [_update()]})
        assert response.status_code == (200 if enabled == "1" else 501)
    assert client.put("/v1/objects/history", json={"updates": []}).status_code == 400
    assert "put" in client.get("/openapi.json").json()["paths"]["/v1/objects/history"]


def test_known_unsupported_history_service_is_not_advertised_or_called(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")
    opcua = fastapi_app(client).state.opcua_client
    opcua.history_update_support = False

    capabilities = client.get("/v1/info").json()["result"]["capabilities"]["update"]
    response = client.put("/v1/objects/history", json={"updates": [_update()]})

    assert capabilities == {"current": True, "history": False}
    assert response.status_code == 501
    assert response.json()["responseDetail"]["status"] == 501
    assert opcua.snapshot_request_metrics().history_write_count == 0
