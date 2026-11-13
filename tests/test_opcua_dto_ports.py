"""Typed-port guards for the OPC UA adapter boundary (PR-09).

Covers the DTO conversion matrix (asyncua ``DataValue``/``Variant`` -> ``DataValueDTO``),
structural conformance of the domain port for the adapter and the shared test double, and
the notification bridge that keeps raw transport objects out of the port callback.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, get_protocol_members

import pytest
from asyncua import ua
from asyncua.ua.status_codes import StatusCodes

from i3x_server.domain.ports.opcua import (
    DataValueDTO,
    OpcUaClientProtocol,
    OpcUaDataChangeHandler,
    OpcUaSubscriptionHandle,
    OpcUaValue,
    Quality,
)
from i3x_server.infrastructure.opcua import values
from i3x_server.infrastructure.opcua.client import OpcUaClient, _NotificationBridge
from i3x_server.infrastructure.subscriptions.monitoring import _NodeDataChangeHandler
from tests.conftest import FakeOpcUaClient


def _server_timestamp(moment: datetime) -> ua.DateTime:
    return ua.DateTime.fromisoformat(moment.astimezone(timezone.utc).isoformat())


def _status_code(severity: int) -> ua.StatusCode:
    return ua.StatusCode(ua.UInt32(severity))


@pytest.mark.parametrize(
    ("variant", "expected_value", "expected_type", "expected_array"),
    [
        (ua.Variant(7, ua.VariantType.Int32), 7, "Int32", False),
        (ua.Variant(42.5, ua.VariantType.Double), 42.5, "Double", False),
        (ua.Variant("on", ua.VariantType.String), "on", "String", False),
        (ua.Variant(True, ua.VariantType.Boolean), True, "Boolean", False),
        (ua.Variant([1.0, 2.0], ua.VariantType.Double), [1.0, 2.0], "Double", True),
        (ua.Variant(None, ua.VariantType.Null), None, "Null", False),
    ],
)
def test_to_data_value_dto_converts_variant_payload(
    variant: ua.Variant, expected_value: Any, expected_type: str, expected_array: bool
) -> None:
    dto = values.to_data_value_dto(ua.DataValue(variant))
    assert dto.value.value == expected_value
    assert dto.value.variant_type == expected_type
    assert dto.value.is_array is expected_array


def test_to_data_value_dto_maps_numeric_status_codes() -> None:
    cases = {
        StatusCodes.Good: ("Good", int(StatusCodes.Good)),
        StatusCodes.GoodNoData: ("GoodNoData", int(StatusCodes.GoodNoData)),
        StatusCodes.Uncertain: ("Uncertain", int(StatusCodes.Uncertain)),
        StatusCodes.Bad: ("Bad", int(StatusCodes.Bad)),
    }
    for code, (expected_quality, expected_int) in cases.items():
        dto = values.to_data_value_dto(ua.DataValue(ua.Variant(1.0, ua.VariantType.Double), _status_code(int(code))))
        assert dto.status_code == expected_int
        assert dto.quality == expected_quality


def test_to_data_value_dto_keeps_source_and_server_timestamps() -> None:
    source = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    server = datetime(2026, 1, 2, 3, 4, 6, tzinfo=timezone.utc)
    data_value = ua.DataValue(ua.Variant(1.0, ua.VariantType.Double))
    data_value.SourceTimestamp = _server_timestamp(source)
    data_value.ServerTimestamp = _server_timestamp(server)
    dto = values.to_data_value_dto(data_value)
    assert dto.source_timestamp == source
    assert dto.server_timestamp == server


def test_to_data_value_dto_is_tolerant_of_partial_test_doubles() -> None:
    """Partially populated doubles convert without raising; missing metadata stays None."""
    dto = values.to_data_value_dto(SimpleNamespace(Value=SimpleNamespace(Value=7)))
    assert dto.value == OpcUaValue(value=7, variant_type=None, is_array=None)
    assert dto.status_code is None
    assert dto.quality is None
    assert dto.source_timestamp is None
    assert dto.server_timestamp is None

    # A bare payload without a Variant wrapper is still the payload.
    bare = values.to_data_value_dto(SimpleNamespace(Value="plain"))
    assert bare.value.value == "plain"

    # Non-datetime timestamps are dropped so consumers fall back to their own clock.
    stamped = values.to_data_value_dto(SimpleNamespace(Value=1, SourceTimestamp="2026-01-01T00:00:00Z"))
    assert stamped.source_timestamp is None

    # A string VariantType (name-only doubles) is surfaced as the type name.
    typed = values.to_data_value_dto(SimpleNamespace(Value=SimpleNamespace(Value=1, VariantType="Double")))
    assert typed.value.variant_type == "Double"


@pytest.mark.parametrize("candidate", [OpcUaClient, FakeOpcUaClient])
def test_client_candidate_satisfies_opcua_port(candidate: type) -> None:
    """The adapter and the shared test double implement every port method."""
    missing = [name for name in get_protocol_members(OpcUaClientProtocol) if not hasattr(candidate, name)]
    assert missing == []


@pytest.mark.parametrize("candidate", [_NodeDataChangeHandler, _NotificationBridge])
def test_handler_candidate_satisfies_handler_port(candidate: type) -> None:
    missing = [name for name in get_protocol_members(OpcUaDataChangeHandler) if not hasattr(candidate, name)]
    assert missing == []


def test_subscription_handle_is_opaque() -> None:
    """The handle protocol carries no members: any object the adapter hands back works."""
    assert get_protocol_members(OpcUaSubscriptionHandle) == frozenset()
    assert isinstance(object(), OpcUaSubscriptionHandle)


class _RecordingHandler:
    def __init__(self) -> None:
        self.updates: list[tuple[str, Any, DataValueDTO | None]] = []
        self.events: list[Any] = []

    def datachange_notification(self, node_id: str, value: Any, data_value: DataValueDTO | None) -> None:
        self.updates.append((node_id, value, data_value))

    def event_notification(self, event: Any) -> None:
        self.events.append(event)


def test_notification_bridge_maps_node_id_and_dto() -> None:
    handler = _RecordingHandler()
    bridge = _NotificationBridge(handler)
    moment = datetime(2026, 4, 5, 6, 7, tzinfo=timezone.utc)
    data_value = ua.DataValue(ua.Variant(9.0, ua.VariantType.Double), _status_code(int(StatusCodes.Uncertain)))
    data_value.SourceTimestamp = _server_timestamp(moment)
    notification = SimpleNamespace(monitored_item=SimpleNamespace(ClientHandle=3, Value=data_value))

    bridge.datachange_notification(
        SimpleNamespace(nodeid=SimpleNamespace(to_string=lambda: "ns=2;s=Temp")), 9.0, notification
    )

    assert handler.updates == [
        (
            "ns=2;s=Temp",
            9.0,
            DataValueDTO(
                value=OpcUaValue(9.0, variant_type="Double", is_array=False),
                status_code=int(StatusCodes.Uncertain),
                quality=Quality.UNCERTAIN,
                source_timestamp=moment,
                server_timestamp=None,
            ),
        )
    ]


def test_notification_bridge_drops_notifications_with_unresolvable_node_id(caplog: pytest.LogCaptureFixture) -> None:
    handler = _RecordingHandler()
    bridge = _NotificationBridge(handler)

    def broken_to_string() -> str:
        raise RuntimeError("nodeid unusable")

    bridge.datachange_notification(SimpleNamespace(nodeid=SimpleNamespace(to_string=broken_to_string)), 1.0, None)

    assert handler.updates == []
    assert "dropping update" in caplog.text


def test_notification_bridge_passes_none_when_notification_has_no_value() -> None:
    handler = _RecordingHandler()
    bridge = _NotificationBridge(handler)

    bridge.datachange_notification(SimpleNamespace(nodeid=SimpleNamespace(to_string=lambda: "ns=2;s=Temp")), 5.0, None)
    bridge.datachange_notification(
        SimpleNamespace(nodeid=SimpleNamespace(to_string=lambda: "ns=2;s=Temp")),
        5.0,
        SimpleNamespace(monitored_item=None),
    )

    assert [data_value for _node_id, _value, data_value in handler.updates] == [None, None]


def test_notification_bridge_forwards_events() -> None:
    handler = _RecordingHandler()
    bridge = _NotificationBridge(handler)
    event = SimpleNamespace(event_fields={"EventId": b"\x01"})
    bridge.event_notification(event)
    assert handler.events == [event]


async def test_create_datachange_subscription_wraps_handler_in_bridge() -> None:
    """The port callback is never handed to asyncua raw: the adapter installs the bridge."""
    handler = _RecordingHandler()

    class _RawClient:
        def __init__(self) -> None:
            self.created: list[Any] = []

        async def create_subscription(self, publishing_interval_ms: float, handler_arg: Any) -> Any:
            del publishing_interval_ms
            self.created.append(handler_arg)
            return SimpleNamespace(id="ua-sub")

    raw = _RawClient()
    client = OpcUaClient("opc.tcp://127.0.0.1:4840")
    client.raw_client = raw  # type: ignore[assignment]

    await client.create_datachange_subscription(1000.0, handler)

    assert len(raw.created) == 1
    assert isinstance(raw.created[0], _NotificationBridge)
    raw.created[0].datachange_notification(
        SimpleNamespace(nodeid=SimpleNamespace(to_string=lambda: "ns=2;s=Temperature")),
        1.5,
        SimpleNamespace(monitored_item=SimpleNamespace(Value=ua.DataValue(ua.Variant(1.5, ua.VariantType.Double)))),
    )
    assert handler.updates[0][0] == "ns=2;s=Temperature"
    # The bridge hands over a DTO for a notification that carries a value, and None otherwise;
    # narrowing here is what makes the quality assertion meaningful rather than incidental.
    data_value = handler.updates[0][2]
    assert data_value is not None
    assert data_value.quality == "Good"
