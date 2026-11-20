"""Upstream failure text must not appear in client responses or server logs."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, cast

import pytest
from asyncua import ua
from asyncua.ua.status_codes import StatusCodes
from asyncua.ua.uaerrors import UaStatusCodeError
from fastapi import HTTPException
from fastapi.testclient import TestClient

from i3x_server.application.errors import ApplicationServiceError
from i3x_server.application.ports.opcua import OpcUaClientProtocol
from i3x_server.application.services.model_query import ModelQueryService
from i3x_server.application.services.object_value import ObjectValueService
from i3x_server.application.services.subscription import SubscriptionAppService
from i3x_server.logging_format import JsonFormatter, TextFormatter, log_upstream_failure
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult
from tests.conftest import fastapi_app

# Text only an upstream could produce. It must never appear in logs or response bodies.
_SECRET = "upstream-detail-4f8a2c: session 17 reset at /srv/opcua/private"


def _assert_no_upstream_text(payload: Any) -> None:
    assert _SECRET not in json.dumps(payload)


def _history_update(value: Any = 19.5) -> dict[str, Any]:
    return {
        "elementId": "property-abc",
        "value": {"value": value, "quality": "Good", "timestamp": "2026-01-01T10:00:00Z"},
    }


def _history_body() -> dict[str, Any]:
    return {
        "elementIds": ["property-abc"],
        "startTime": "2026-01-01T00:00:00Z",
        "endTime": "2026-01-02T00:00:00Z",
    }


def _fail_on(app: Any, method: str, exc: Exception) -> None:
    """Make the fake client raise ``exc`` for one upstream method."""

    async def _raise(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise exc

    setattr(app.state.opcua_client, method, _raise)


def _status_error(code: int) -> UaStatusCodeError:
    """The asyncua error an upstream server raises for status ``code``."""
    try:
        ua.StatusCode(ua.UInt32(code)).check()
    except UaStatusCodeError as exc:
        return exc
    raise AssertionError(f"status code {code} does not raise a UaStatusCodeError")


def test_ua_status_failure_hides_cause_from_server_logs(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    app = fastapi_app(client)
    _fail_on(app, "read_server_status_data_value", RuntimeError(_SECRET))
    with caplog.at_level(logging.WARNING):
        response = client.get("/ua/status")
    assert response.status_code == 502
    payload = response.json()
    assert payload["error"]["message"] == "Failed to read OPC UA ServerStatus"
    assert payload["responseDetail"]["detail"] == "Failed to read OPC UA ServerStatus"
    _assert_no_upstream_text(payload)
    assert _SECRET not in caplog.text


def test_ua_connection_failure_hides_cause_from_server_logs(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    app = fastapi_app(client)

    def _raise() -> Any:
        raise RuntimeError(_SECRET)

    app.state.opcua_client.get_connection_snapshot = _raise
    with caplog.at_level(logging.WARNING):
        response = client.get("/ua/connection")
    assert response.status_code == 500
    payload = response.json()
    assert payload["error"]["message"] == "Failed to resolve OPC UA connection state"
    assert payload["responseDetail"]["detail"] == "Failed to resolve OPC UA connection state"
    _assert_no_upstream_text(payload)
    assert _SECRET not in caplog.text


def test_ua_limits_failure_hides_cause_from_server_logs(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    app = fastapi_app(client)
    _fail_on(app, "get_operational_limits", RuntimeError(_SECRET))
    with caplog.at_level(logging.WARNING):
        response = client.get("/ua/limits")
    assert response.status_code == 502
    payload = response.json()
    assert payload["error"]["message"] == "Failed to read OPC UA limits and capabilities"
    assert payload["responseDetail"]["detail"] == "Failed to read OPC UA limits and capabilities"
    _assert_no_upstream_text(payload)
    assert _SECRET not in caplog.text


def test_namespaces_failure_hides_cause_from_server_logs(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    app = fastapi_app(client)
    _fail_on(app, "get_namespace_infos", RuntimeError(_SECRET))
    with caplog.at_level(logging.WARNING):
        response = client.get("/v1/namespaces")
    assert response.status_code == 502
    payload = response.json()
    assert payload["error"]["message"] == "Failed to read OPC UA namespaces"
    assert payload["responseDetail"]["detail"] == "Failed to read OPC UA namespaces"
    _assert_no_upstream_text(payload)
    assert _SECRET not in caplog.text


def test_objects_failure_hides_cause_from_server_logs(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    """`GET /v1/objects` reports the namespace discovery failure without the upstream cause."""
    app = fastapi_app(client)
    _fail_on(app, "get_namespace_infos", RuntimeError(_SECRET))
    with caplog.at_level(logging.WARNING):
        response = client.get("/v1/objects")
    assert response.status_code == 502
    payload = response.json()
    assert payload["error"]["message"] == "Failed to read OPC UA namespaces"
    assert payload["responseDetail"]["detail"] == "Failed to read OPC UA namespaces"
    _assert_no_upstream_text(payload)
    assert _SECRET not in caplog.text


def test_objecttypes_failure_hides_cause_from_server_logs(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    app = fastapi_app(client)
    _fail_on(app, "get_object_types", RuntimeError(_SECRET))
    with caplog.at_level(logging.WARNING):
        response = client.get("/v1/objecttypes")
    assert response.status_code == 502
    payload = response.json()
    assert payload["error"]["message"] == "Failed to read object types"
    assert payload["responseDetail"]["detail"] == "Failed to read object types"
    _assert_no_upstream_text(payload)
    assert _SECRET not in caplog.text


def test_value_read_failure_hides_cause_from_server_logs(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    app = fastapi_app(client)
    _fail_on(app, "read_data_values", RuntimeError(_SECRET))
    with caplog.at_level(logging.WARNING):
        response = client.post("/v1/objects/value", json={"elementIds": ["property-abc"]})
    assert response.status_code == 502
    payload = response.json()
    assert payload["error"]["message"] == "Failed to read values"
    assert payload["responseDetail"]["detail"] == "Failed to read values"
    _assert_no_upstream_text(payload)
    assert _SECRET not in caplog.text


def test_history_read_failure_hides_cause_from_server_logs(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    app = fastapi_app(client)
    _fail_on(app, "read_history_values", RuntimeError(_SECRET))
    with caplog.at_level(logging.WARNING):
        response = client.post("/v1/objects/history", json=_history_body())
    assert response.status_code == 502
    payload = response.json()
    assert payload["error"]["message"] == "Failed to read historical values"
    assert payload["responseDetail"]["detail"] == "Failed to read historical values"
    _assert_no_upstream_text(payload)
    assert _SECRET not in caplog.text


def test_subscription_create_failure_hides_cause_from_server_logs(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    app = fastapi_app(client)

    async def _raise(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise RuntimeError(_SECRET)

    app.state.subscription_service.create_subscription = _raise
    with caplog.at_level(logging.WARNING):
        response = client.post("/v1/subscriptions", json={"clientId": "client-a"})
    assert response.status_code == 502
    payload = response.json()
    assert payload["error"]["message"] == "Failed to create subscription"
    assert payload["responseDetail"]["detail"] == "Failed to create subscription"
    _assert_no_upstream_text(payload)
    assert _SECRET not in caplog.text


@pytest.mark.parametrize(
    ("failure", "status", "message"),
    [
        # Upstream wording must not appear in the log or the item message.
        (_status_error(StatusCodes.BadHistoryOperationUnsupported), 501, "OPC UA historical writes unsupported"),
        (_status_error(StatusCodes.BadNodeIdUnknown), 404, "OPC UA historical write target node not found"),
        (_status_error(StatusCodes.BadHistoryOperationInvalid), 400, "OPC UA historical write rejected by the server"),
        (_status_error(StatusCodes.BadInvalidArgument), 400, "OPC UA historical write rejected by the server"),
        (_status_error(StatusCodes.BadTypeMismatch), 400, "bad_type_or_range"),
        (_status_error(StatusCodes.BadNotWritable), 403, "target_not_writable"),
        (RuntimeError(_SECRET), 502, "opcua_write_failed"),
        (TimeoutError(_SECRET), 502, "session_or_transport_failure"),
    ],
)
def test_history_write_items_hide_upstream_text(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
    status: int,
    message: str,
) -> None:
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")
    app = fastapi_app(client)
    app.state.opcua_client.history_write_failures["ns=2;s=Temperature"] = failure
    upstream_text = str(failure)
    with caplog.at_level(logging.WARNING):
        response = client.put("/v1/objects/history", json={"updates": [_history_update()]})
    assert response.status_code == 200
    payload = response.json()
    item = payload["results"][0]
    assert item["success"] is False
    assert item["error"]["code"] == status
    assert item["error"]["message"] == message
    assert item["responseDetail"]["detail"] == message
    assert upstream_text not in json.dumps(payload)
    assert upstream_text not in caplog.text


def _model() -> BuildResult:
    node = ModelNode(
        id="prop-a",
        name="Temperature",
        kind="property",
        type="Double",
        children=[],
        source_node_id="ns=2;s=Temperature",
    )
    return BuildResult(
        nodes_by_id={node.id: node},
        root_ids=[node.id],
        children_by_id={node.id: []},
        instances_by_type_id={},
        property_to_node={node.id: node.source_node_id},
        action_to_method={},
    )


class _ReadFailingClient:
    """Client whose batched reads fail with sensitive text."""

    def __init__(self, cause: Exception) -> None:
        self._cause = cause

    async def read_data_values(self, node_ids: list[str]) -> Any:
        del node_ids
        raise self._cause

    async def read_history_values(
        self,
        node_ids: list[str],
        start_time: datetime,
        end_time: datetime,
    ) -> Any:
        del node_ids, start_time, end_time
        raise self._cause


@pytest.mark.asyncio
async def test_object_value_service_read_failure_hides_cause_from_server_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = ObjectValueService(cast(OpcUaClientProtocol, _ReadFailingClient(RuntimeError(_SECRET))), _model())
    with caplog.at_level(logging.WARNING):
        with pytest.raises(HTTPException) as exc_info:
            await service.get_current_value("prop-a")
    assert exc_info.value.status_code == 502
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["error"]["message"] == "Failed to read current value for prop-a"
    assert detail["responseDetail"]["detail"] == "Failed to read current value for prop-a"
    _assert_no_upstream_text(detail)
    assert _SECRET not in caplog.text


@pytest.mark.asyncio
async def test_object_value_service_history_failure_hides_cause_from_server_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = ObjectValueService(cast(OpcUaClientProtocol, _ReadFailingClient(RuntimeError(_SECRET))), _model())
    with caplog.at_level(logging.WARNING):
        with pytest.raises(HTTPException) as exc_info:
            await service.get_history("prop-a", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
    assert exc_info.value.status_code == 502
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["error"]["message"] == "Failed to read history for prop-a"
    assert detail["responseDetail"]["detail"] == "Failed to read history for prop-a"
    _assert_no_upstream_text(detail)
    assert _SECRET not in caplog.text


class _NamespaceFailingClient:
    async def get_namespace_infos(self) -> Any:
        raise RuntimeError(_SECRET)


@pytest.mark.asyncio
async def test_model_query_service_namespace_failure_hides_cause_from_server_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = ModelQueryService(cast(OpcUaClientProtocol, _NamespaceFailingClient()), _model())
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ApplicationServiceError) as exc_info:
            await service.get_namespaces()
    assert exc_info.value.status_code == 502
    assert exc_info.value.error_code == "OpcUaNamespaceError"
    assert exc_info.value.details == {}
    assert _SECRET not in caplog.text


class _ObjectTypesFailingClient(_NamespaceFailingClient):
    async def get_object_types(self) -> Any:
        raise RuntimeError(_SECRET)


@pytest.mark.asyncio
async def test_model_query_service_object_type_failure_hides_cause_from_server_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = ModelQueryService(cast(OpcUaClientProtocol, _ObjectTypesFailingClient()), _model())
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ApplicationServiceError) as exc_info:
            await service.get_object_types()
    assert exc_info.value.status_code == 502
    assert exc_info.value.error_code == "OpcUaObjectTypeError"
    assert exc_info.value.details == {}
    assert _SECRET not in caplog.text


class _SubscriptionFailingService:
    async def create_subscription(self, client_id: str, display_name: str | None = None) -> Any:
        del client_id, display_name
        raise RuntimeError(_SECRET)

    async def list_subscriptions(
        self,
        client_id: str | None = None,
        subscription_ids: list[str] | None = None,
    ) -> Any:
        del client_id, subscription_ids
        raise RuntimeError(_SECRET)


@pytest.mark.asyncio
async def test_subscription_app_service_wraps_without_upstream_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = SubscriptionAppService(
        cast(OpcUaClientProtocol, object()),
        _model(),
        cast(Any, _SubscriptionFailingService()),
    )
    with caplog.at_level(logging.WARNING):
        with pytest.raises(HTTPException) as exc_info:
            await service.list_subscriptions(client_id="client-a")
    assert exc_info.value.status_code == 502
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["error"]["message"] == "Failed to list subscriptions"
    assert detail["responseDetail"]["detail"] == "Failed to list subscriptions"
    _assert_no_upstream_text(detail)
    assert _SECRET not in caplog.text


def test_gateway_owned_diagnostics_still_reach_the_client(client: TestClient) -> None:
    """Sanitizing upstream text must not remove the gateway's own field/value diagnostics."""
    app = fastapi_app(client)

    async def _raise_service_error() -> None:
        raise ApplicationServiceError(
            400,
            "InvalidArgument",
            "Invalid value for 'maxDepth'",
            {"field": "maxDepth", "value": 0},
        )

    app.add_api_route("/test/gateway-diagnostic", _raise_service_error, methods=["GET"])
    response = client.get("/test/gateway-diagnostic")
    assert response.status_code == 400
    payload = response.json()
    assert payload["responseDetail"]["detail"] == "Invalid value for 'maxDepth' (field=maxDepth, value=0)"


def test_history_write_audit_identity_is_not_leaked(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-item message stays a stable code, so the audit identity cannot forge it."""
    monkeypatch.setenv("I3X_ENABLE_WRITES", "1")
    app = fastapi_app(client)
    app.state.opcua_client.history_user_writable_by_node_id["ns=2;s=Temperature"] = False
    response = client.put(
        "/v1/objects/history",
        json={"updates": [_history_update()]},
        headers={"x-principal": "forged-identity"},
    )
    payload = response.json()
    assert payload["results"][0]["error"]["code"] == 403
    assert "forged-identity" not in json.dumps(payload)


def test_log_upstream_failure_records_both_cause_shapes(caplog: pytest.LogCaptureFixture) -> None:
    """Neither cause text nor chained exception details reach text or JSON logs."""
    try:
        try:
            raise ValueError(_SECRET)
        except ValueError as exc:
            raise RuntimeError(_SECRET) from exc
    except RuntimeError as cause:
        exception = cause
    with caplog.at_level(logging.WARNING, logger="i3x_server.upstream"):
        log_upstream_failure("OpcUaError", "Failed to read values", exception)
        log_upstream_failure("OpcUaError", "Failed to read values", _SECRET)
    assert [record.getMessage() for record in caplog.records] == [
        "upstream failure code=OpcUaError message=Failed to read values cause_type=RuntimeError",
        "upstream failure code=OpcUaError message=Failed to read values cause_type=str",
    ]
    for record in caplog.records:
        assert record.exc_info is None
        assert _SECRET not in TextFormatter().format(record)
        assert _SECRET not in JsonFormatter().format(record)
