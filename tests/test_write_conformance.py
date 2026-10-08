from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from asyncua import ua
from asyncua.ua.status_codes import StatusCodes
from asyncua.ua.uaerrors import UaStatusCodeError
from fastapi.testclient import TestClient

from i3x_server.api.v1.contracts import HistoryWriteVQTRequest
from i3x_server.api.v1.object_helpers import _resolved_type_element_id_for_node
from tests.conftest import fastapi_app


def _publish_schema(client: TestClient, schema: dict[str, Any]) -> None:
    assert client.get("/v1/objecttypes").status_code == 200
    app = fastapi_app(client)
    context = app.state.object_type_context_cache["context"]
    type_id = _resolved_type_element_id_for_node(
        app.state.model_cache.nodes_by_id["property-abc"],
        context.namespace_infos,
        context.element_ids_by_node_id,
        context.source_type_to_element_id,
    )
    for item in context.items:
        if item.elementId == type_id:
            item.schema_ = schema
            break
    else:
        pytest.fail("Property type missing from published registry")
    listed = client.get("/v1/objecttypes").json()["result"]
    assert next(item for item in listed if item["elementId"] == type_id)["schema"] == schema


def _write(client: TestClient, operation: str, value: Any, element_id: str = "property-abc") -> Any:
    vqt = {"value": value, "quality": "Bad" if value is None else "Good", "timestamp": "2026-01-01T10:00:00Z"}
    return client.put(f"/v1/objects/{operation}", json={"updates": [{"elementId": element_id, "value": vqt}]})


@pytest.mark.parametrize("operation", ["value", "history"])
@pytest.mark.parametrize(
    ("schema", "value", "expected"),
    [
        ({"type": "number", "minimum": 10, "maximum": 20}, 15, True),
        ({"type": "number", "minimum": 10}, 5, False),
        ({"type": "number", "maximum": 20}, 25, False),
        ({"type": "number", "enum": [10, 20]}, 15, False),
        ({"type": "number", "enum": [10, 20]}, 20, True),
        ({"type": "number"}, None, False),
        ({"type": ["number", "null"]}, None, True),
        ({"type": "string", "format": "date-time"}, "not-a-date", False),
        ({"type": "object", "properties": {"a": {"type": "number"}}, "required": ["a"]}, {}, False),
        ({"type": "object", "properties": {"a": {"type": "number"}}, "required": ["a"]}, {"a": "bad"}, False),
        ({"type": "object", "properties": {"a": {"type": "number"}}, "additionalProperties": False}, {"b": 1}, False),
        ({"$defs": {"value": {"type": "number", "enum": [20]}}, "$ref": "#/$defs/value"}, 15, False),
        ({"$defs": {"value": {"type": "number", "enum": [20]}}, "$ref": "#/$defs/value"}, 20, True),
    ],
)
def test_both_write_paths_validate_published_schema(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    schema: dict[str, Any],
    value: Any,
    expected: bool,
) -> None:
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")
    _publish_schema(client, schema)
    opcua = fastapi_app(client).state.opcua_client
    current_write = AsyncMock(wraps=opcua.write_value)
    history_write = AsyncMock(wraps=opcua.write_history_value)
    monkeypatch.setattr(opcua, "write_value", current_write)
    monkeypatch.setattr(opcua, "write_history_value", history_write)
    response = _write(client, operation, value)
    assert response.status_code == 200
    assert response.json()["success"] is expected
    if not expected:
        failure = response.json()["results"][0]
        assert failure["responseDetail"]["status"] == 400
        assert "ObjectType" in failure["responseDetail"]["detail"]
        current_write.assert_not_awaited()
        history_write.assert_not_awaited()
    elif operation == "history":
        history_write.assert_awaited_once()
    else:
        current_write.assert_awaited_once()


@pytest.mark.parametrize("operation", ["value", "history"])
def test_schema_accepts_a_valid_datetime(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")
    _publish_schema(client, {"type": "string", "format": "date-time"})
    fastapi_app(client).state.opcua_client.variant_type_by_node_id["ns=2;s=Temperature"] = "DateTime"
    response = _write(client, operation, "2026-01-01T00:00:00Z")
    assert response.status_code == 200
    assert response.json()["success"] is True


@pytest.mark.parametrize("operation", ["value", "history"])
def test_schema_rejects_even_a_current_noop(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")
    _publish_schema(client, {"type": "number", "maximum": 20})
    opcua = fastapi_app(client).state.opcua_client
    opcua.writable_by_node_id["ns=2;s=Temperature"] = False
    opcua.read_data_values = AsyncMock()
    response = _write(client, operation, 42.5)
    assert response.json()["success"] is False
    opcua.read_data_values.assert_not_awaited()


@pytest.mark.parametrize("operation", ["value", "history"])
@pytest.mark.parametrize("mode", ["discovery", "missing", "invalid", "external", "unresolved"])
def test_schema_resolution_failure_never_writes(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    mode: str,
) -> None:
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")
    opcua = fastapi_app(client).state.opcua_client
    if mode == "discovery":
        monkeypatch.setattr(opcua, "get_object_types", AsyncMock(side_effect=RuntimeError("discovery failed")))
    else:
        _publish_schema(
            client,
            {
                "missing": {"type": "number"},
                "invalid": {"type": "not-a-json-schema-type"},
                "external": {"$ref": "https://invalid.example/schema"},
                "unresolved": {"$ref": "#/$defs/missing"},
            }[mode],
        )
        if mode == "missing":
            fastapi_app(client).state.object_type_context_cache["context"].items.clear()
    write = AsyncMock()
    history_write = AsyncMock()
    monkeypatch.setattr(opcua, "write_value", write)
    monkeypatch.setattr(opcua, "write_history_value", history_write)
    response = _write(client, operation, 19.5)
    assert response.status_code == 200
    failure = response.json()["results"][0]
    assert failure["responseDetail"]["status"] == 502
    write.assert_not_awaited()
    history_write.assert_not_awaited()


@pytest.mark.parametrize("operation", ["value", "history"])
@pytest.mark.parametrize("element_id", [" property-abc", "property-abc ", "property\x01abc"])
def test_write_element_id_syntax(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    element_id: str,
) -> None:
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")
    assert _write(client, operation, 19.5, element_id).status_code == 400


@pytest.mark.parametrize(
    ("timestamp", "expected_microseconds"),
    [
        ("2026-01-01T00:00:00Z", 0),
        ("2026-01-01T00:00:00.123456Z", 123456),
        ("2026-01-01T00:00:00.123456000Z", 123456),
        ("2026-01-01T00:00:00.000000000Z", 0),
    ],
)
def test_history_timestamp_preserves_exact_precision(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    timestamp: str,
    expected_microseconds: int,
) -> None:
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")
    payload = HistoryWriteVQTRequest.model_validate({"value": 19.5, "quality": "Good", "timestamp": timestamp})
    assert payload.timestamp == datetime(2026, 1, 1, microsecond=expected_microseconds, tzinfo=timezone.utc)
    response = client.put(
        "/v1/objects/history",
        json={
            "updates": [
                {
                    "elementId": "property-abc",
                    "value": {"value": 19.5, "quality": "Good", "timestamp": timestamp},
                }
            ]
        },
    )
    assert response.json()["success"] is True
    records = fastapi_app(client).state.opcua_client.history_values["ns=2;s=Temperature"]
    assert any(record.SourceTimestamp == payload.timestamp for record in records)


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-01-01T00:00:00+01:00",
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:00:00-00:00",
        "2026-01-01T00:00:00.1234567Z",
        "2026-01-01T00:00:00.000000001Z",
    ],
)
def test_history_timestamp_rejects_offsets_and_precision_loss(client: TestClient, timestamp: str) -> None:
    response = client.put(
        "/v1/objects/history",
        json={
            "updates": [
                {
                    "elementId": "property-abc",
                    "value": {"value": 19.5, "quality": "Good", "timestamp": timestamp},
                }
            ]
        },
    )
    assert response.status_code == 400


def _status_error(code: int) -> UaStatusCodeError:
    with pytest.raises(UaStatusCodeError) as caught:
        ua.StatusCode(ua.UInt32(code)).check()
    return caught.value


@pytest.mark.parametrize(
    "service_code,expected_http",
    [
        (StatusCodes.BadServiceUnsupported, 501),
        (StatusCodes.BadHistoryOperationUnsupported, 200),
        (StatusCodes.BadNotWritable, 200),
    ],
)
def test_only_explicit_service_unavailability_returns_http_501(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    service_code: int,
    expected_http: int,
) -> None:
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")
    opcua = fastapi_app(client).state.opcua_client
    write = AsyncMock(side_effect=_status_error(service_code))
    monkeypatch.setattr(opcua, "write_history_value", write)
    updates = [
        {
            "elementId": item,
            "value": {
                "value": value,
                "quality": "Good",
                "timestamp": "2026-01-01T10:00:00Z",
            },
        }
        for item, value in [("property-abc", 19.5), ("missing", 1), ("property-abc", 20)]
    ]
    response = client.put("/v1/objects/history", json={"updates": updates})
    assert response.status_code == expected_http
    assert [item["elementId"] for item in response.json()["results"]] == [item["elementId"] for item in updates]
    assert len(response.json()["results"]) == 3
    assert response.json()["success"] is False
    if expected_http == 501:
        assert response.json()["responseDetail"]["status"] == 501
        assert write.await_count == 1
    else:
        assert write.await_count == 2


def test_service_unsupported_after_success_preserves_partial_outcomes(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")
    write = AsyncMock(side_effect=[None, _status_error(StatusCodes.BadServiceUnsupported)])
    monkeypatch.setattr(fastapi_app(client).state.opcua_client, "write_history_value", write)
    updates = [
        {
            "elementId": "property-abc",
            "value": {
                "value": value,
                "quality": "Good",
                "timestamp": "2026-01-01T10:00:00Z",
            },
        }
        for value in (19.5, 20, 21)
    ]
    response = client.put("/v1/objects/history", json={"updates": updates})
    assert response.status_code == 200
    assert response.json()["success"] is False
    assert [item["success"] for item in response.json()["results"]] == [True, False, False]
    assert write.await_count == 2


def test_unsupported_access_read_does_not_disable_history_service(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")
    opcua = fastapi_app(client).state.opcua_client
    read_access = AsyncMock(side_effect=[_status_error(StatusCodes.BadServiceUnsupported), (True, True)])
    write = AsyncMock()
    monkeypatch.setattr(opcua, "read_history_write_access", read_access)
    monkeypatch.setattr(opcua, "write_history_value", write)
    updates = [
        {
            "elementId": "property-abc",
            "value": {"value": value, "quality": "Good", "timestamp": "2026-01-01T10:00:00Z"},
        }
        for value in (19.5, 20)
    ]
    response = client.put("/v1/objects/history", json={"updates": updates})
    assert response.status_code == 200
    assert [item["success"] for item in response.json()["results"]] == [False, True]
    assert response.json()["results"][0]["responseDetail"]["status"] == 502
    assert read_access.await_count == 2
    write.assert_awaited_once()
