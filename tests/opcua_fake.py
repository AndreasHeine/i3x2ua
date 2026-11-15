"""Canonical shared OPC UA client doubles for the ``OpcUaClientProtocol`` port.

One double for all port-level consumers replaces the per-module forks (conftest, ``test_api``,
``test_subscription_service``, port-conformance stubs): it implements every protocol member with
the port's own DTO types, so strict mypy verifies structural conformance without ``cast``, and
it records request bodies/counters so tests can assert behavior rather than method presence.

``SubscriptionClientFake`` is the subscription-runtime variant: it mirrors a server with generous
limits (batching 100 items per call) and index-based read values, which the subscription service
tests rely on; all other behavior is inherited from the canonical double.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from i3x_server.domain.ports.opcua import (
    DataValueDTO,
    OpcUaConnectionSnapshot,
    OpcUaNamespaceInfo,
    OpcUaNodeInfo,
    OpcUaObjectTypeInfo,
    OpcUaObjectTypeMemberInfo,
    OpcUaOperationalLimits,
    OpcUaRequestMetrics,
    OpcUaRuntimeMetrics,
    OpcUaSubscriptionCapabilities,
    OpcUaSubscriptionHandle,
    OpcUaTypeDefinitionInfo,
    OpcUaValue,
)
from i3x_server.schemas.opcua_value import quality_from_status_code

# Numeric OPC UA codes for the i3X quality vocabulary. The DTO carries both the code
# (severity mapping) and the adapter-decoded quality (keeps GoodNoData distinct), so the
# double answers exactly like the adapter conversion does.
_QUALITY_STATUS_CODES = {
    "Good": 0x00000000,
    "GoodNoData": 0x00020000,
    "Uncertain": 0x40000000,
    "Bad": 0x80000000,
}

# ``DataValueDTO.source_timestamp`` is optional, so a sort key over it is not total. Every
# history record the double writes carries a timezone-aware timestamp; this fallback only keeps
# the comparison total (and aware, never naive) if a record without one ever slips in.
_HISTORY_SORT_FALLBACK = datetime.max.replace(tzinfo=timezone.utc)


def data_value_dto(value: Any, quality: str = "Good", timestamp: datetime | None = None) -> DataValueDTO:
    """Build a data-value DTO like the adapter would hand over for ``value``/``quality``."""
    status_code = _QUALITY_STATUS_CODES[quality]
    return DataValueDTO(
        value=OpcUaValue(value=value),
        status_code=status_code,
        quality=quality_from_status_code(SimpleNamespace(value=status_code)),
        source_timestamp=timestamp,
    )


@dataclass(slots=True)
class FakeMachineThresholds:
    min: float
    max: float


@dataclass(slots=True)
class FakeMachineConfig:
    thresholds: FakeMachineThresholds
    mode: str


class FakeExtensionObject:
    def __init__(self, type_id: str, body: Any) -> None:
        self.TypeId = type_id
        self.Body = body


_DEFAULT_NAMESPACE_INFOS = [
    OpcUaNamespaceInfo(
        uri="http://example.com/i3x",
        display_name="I3X",
        namespace_version="1.0",
        server_uri="urn:test-server",
    ),
    OpcUaNamespaceInfo(
        uri="http://example.com/custom",
        display_name="Custom",
        namespace_version="1.0",
        server_uri="urn:test-server",
    ),
    OpcUaNamespaceInfo(
        uri="http://example.com/runtime",
        display_name="Runtime",
        namespace_version="1.0",
        server_uri="urn:test-server",
    ),
]


def _default_object_types() -> list[OpcUaObjectTypeInfo]:
    """Fresh DTO bodies per construction: tests must not mutate shared fixtures."""
    return [
        OpcUaObjectTypeInfo(
            node_id="ns=1;i=1001",
            parent_node_id=None,
            browse_name="MachineType",
            display_name="Machine Type",
            properties={"temperature": "i=11", "running": "i=1", "config": "ns=1;i=3001"},
            description="Machine object type",
            is_abstract=False,
            members=[
                OpcUaObjectTypeMemberInfo(
                    node_id="ns=1;i=2001",
                    browse_name="temperature",
                    display_name="Temperature",
                    description="Current measured temperature",
                    node_class="Variable",
                    data_type="i=11",
                    value=42.5,
                    modelling_rule="Mandatory",
                ),
                OpcUaObjectTypeMemberInfo(
                    node_id="ns=1;i=2002",
                    browse_name="running",
                    display_name="Running",
                    description="Running state",
                    node_class="Variable",
                    data_type="i=1",
                    value=True,
                ),
                OpcUaObjectTypeMemberInfo(
                    node_id="ns=1;i=2003",
                    browse_name="config",
                    display_name="Config",
                    description="Machine configuration",
                    node_class="Variable",
                    data_type="ns=1;i=3001",
                    value=FakeExtensionObject(
                        "ns=1;i=3001",
                        FakeMachineConfig(thresholds=FakeMachineThresholds(min=10.0, max=120.5), mode="auto"),
                    ),
                    # The adapter hands the schema builder ``schema_value``; the schema builder falls
                    # back to ``value`` only when the attribute is absent, so typed DTO members must
                    # carry the extension body here explicitly.
                    schema_value=FakeExtensionObject(
                        "ns=1;i=3001",
                        FakeMachineConfig(thresholds=FakeMachineThresholds(min=10.0, max=120.5), mode="auto"),
                    ),
                ),
            ],
        ),
        OpcUaObjectTypeInfo(
            node_id="ns=1;i=1002",
            parent_node_id="ns=1;i=1001",
            browse_name="SensorType",
            display_name="Sensor Type",
            properties={},
            description="Sensor subtype",
            is_abstract=True,
            members=[],
        ),
    ]


class FakeOpcUaClient:
    """Canonical ``OpcUaClientProtocol`` double with recorded request bodies and counters."""

    def __init__(
        self,
        *,
        namespace_infos: list[OpcUaNamespaceInfo] | None = None,
        object_types: list[OpcUaObjectTypeInfo] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._namespace_infos = namespace_infos if namespace_infos is not None else list(_DEFAULT_NAMESPACE_INFOS)
        self.object_types = object_types if object_types is not None else _default_object_types()
        self._error = error
        self.values: dict[str, Any] = {"ns=2;s=Temperature": 42.5}
        self.writable_by_node_id: dict[str, bool] = {"ns=2;s=Temperature": True}
        self.user_writable_by_node_id: dict[str, bool] = {"ns=2;s=Temperature": True}
        self.variant_type_by_node_id: dict[str, str] = {"ns=2;s=Temperature": "Double"}
        self.last_write_vqt_by_node_id: dict[str, tuple[str, datetime]] = {}
        self.last_write_variant_type_by_node_id: dict[str, str | None] = {}
        self.write_failures: dict[str, Exception] = {}
        self.history_writable_by_node_id: dict[str, bool] = {"ns=2;s=Temperature": True}
        self.history_user_writable_by_node_id: dict[str, bool] = {"ns=2;s=Temperature": True}
        self.history_write_failures: dict[str, Exception] = {}
        self.history_update_support: bool | None = None
        self.history_values: dict[str, list[DataValueDTO]] = {
            "ns=2;s=Temperature": [
                data_value_dto(40.0, "Good", datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)),
                data_value_dto(41.5, "Good", datetime(2026, 1, 1, 10, 5, tzinfo=timezone.utc)),
            ]
        }
        self.read_values_calls = 0
        self.read_data_values_calls = 0
        self.server_status_reads = 0
        self._reads = 0
        # Subscription instrumentation: handles, handlers, and per-call recordings.
        self.handlers: list[Any] = []
        self.create_subscription_calls = 0
        self.subscribe_calls = 0
        self.unsubscribed_calls: list[tuple[OpcUaSubscriptionHandle, list[int]]] = []
        self.deleted_subscriptions: list[OpcUaSubscriptionHandle] = []
        # node_id -> DTO to serve instead of the generated Good value.
        self.data_value_overrides: dict[str, DataValueDTO] = {}
        # id(ua_subscription) -> handles the double reports after a reconnect (None = lost).
        self.restored_handles: dict[int, dict[str, int] | None] = {}
        self._next_handle = 1
        self.listeners: list[Any] = []
        self.removed_listeners: list[Any] = []
        self._connection_snapshot = OpcUaConnectionSnapshot(
            state="Connected",
            endpoint="opc.tcp://opcua.umati.app:4843",
            since=datetime(2026, 1, 1, 8, 11, 24, tzinfo=timezone.utc),
        )
        self._request_metrics = OpcUaRequestMetrics(
            read_count=2451,
            write_count=0,
            browse_count=189,
            method_call_count=27,
            history_read_count=62,
            history_write_count=0,
            failed_request_count=14,
            goodish_qualities=["Good", "Uncertain"],
        )
        self._runtime_metrics = OpcUaRuntimeMetrics()
        self._operational_limits = OpcUaOperationalLimits(
            max_nodes_per_browse=1000,
            max_nodes_per_read=1000,
        )

    async def get_namespace_infos(self) -> list[OpcUaNamespaceInfo]:
        if self._error is not None:
            raise self._error
        self._runtime_metrics.namespace_reads += 1
        self._runtime_metrics.namespace_info_count_last = len(self._namespace_infos)
        return self._namespace_infos

    async def get_namespaces(self) -> list[str]:
        return [info.uri for info in await self.get_namespace_infos()]

    async def get_object_types(self) -> list[OpcUaObjectTypeInfo]:
        if self._error is not None:
            raise self._error
        self._runtime_metrics.object_type_reads += 1
        self._runtime_metrics.object_type_count_last = len(self.object_types)
        return self.object_types

    async def browse_tree(self) -> list[OpcUaNodeInfo]:
        # The model is built from the fixture's static node table, not from a tree walk.
        return []

    async def resolve_type_definition_hierarchy(self, type_definition_id: str) -> list[OpcUaTypeDefinitionInfo]:
        del type_definition_id
        return []

    async def resolve_type_definition_hierarchies(
        self, type_definition_ids: list[str]
    ) -> dict[str, list[OpcUaTypeDefinitionInfo]]:
        return {type_definition_id: [] for type_definition_id in type_definition_ids}

    async def read_value(self, node_id: str) -> Any:
        return self.values[node_id]

    async def read_browse_name(self, node_id: str) -> str | None:
        if node_id == "ns=0;i=17364":
            return "NetworkAddressDataType"
        if node_id == "ns=0;i=865":
            return "SessionDiagnosticsDataType"
        if node_id == "ns=0;i=96":
            return "RolePermissionType"
        if node_id == "nsu=http://example.com/custom;i=900001":
            return "CustomNetworkAddressType"
        if node_id == "nsu=http://example.com/custom;i=900002":
            return "CustomMachineStateType"
        return None

    async def read_values(self, node_ids: list[str]) -> list[Any]:
        self._reads += 1
        self.read_values_calls += 1
        results: list[Any] = []
        for node_id in node_ids:
            base = self.values.get(node_id, 1.0)
            if isinstance(base, (bytes, bytearray, memoryview)):
                results.append(bytes(base))
                continue
            value = float(base) + self._reads
            self.values[node_id] = value
            results.append(value)
        return results

    async def read_data_values(self, node_ids: list[str]) -> list[DataValueDTO]:
        self._reads += 1
        self.read_data_values_calls += 1
        results: list[DataValueDTO] = []
        for node_id in node_ids:
            override = self.data_value_overrides.get(node_id)
            if override is not None:
                results.append(override)
                continue
            base = self.values.get(node_id, 1.0)
            # Mirrors a server that preserves the last written VQT: the bulk write path
            # verifies quality and source timestamp through this batched read. Written
            # values are answered verbatim (no simulated drift), so a verification read
            # does not alter the stored value.
            written = self.last_write_vqt_by_node_id.get(node_id)
            if written is not None:
                quality, source_timestamp = written
                value = bytes(base) if isinstance(base, (bytes, bytearray, memoryview)) else base
                results.append(data_value_dto(value, quality, source_timestamp))
                continue
            read_value: Any
            if isinstance(base, (bytes, bytearray, memoryview)):
                read_value = bytes(base)
            else:
                read_value = float(base) + self._reads
                self.values[node_id] = read_value
            results.append(data_value_dto(read_value, "Good", datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)))
        return results

    async def read_history_values(
        self,
        node_ids: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> dict[str, list[DataValueDTO]]:
        del start_time, end_time
        return {node_id: self.history_values.get(node_id, []) for node_id in node_ids}

    async def read_write_access(self, node_id: str) -> tuple[bool, bool]:
        return self.writable_by_node_id.get(node_id, False), self.user_writable_by_node_id.get(node_id, False)

    async def read_history_write_access(self, node_id: str) -> tuple[bool, bool]:
        return (
            self.history_writable_by_node_id.get(node_id, False),
            self.history_user_writable_by_node_id.get(node_id, False),
        )

    async def read_variant_type(self, node_id: str) -> str | None:
        return self.variant_type_by_node_id.get(node_id)

    def history_update_supported(self) -> bool | None:
        return self.history_update_support

    async def write_history_value(self, node_id: str, value: Any, quality: str, timestamp: datetime) -> None:
        failure = self.history_write_failures.get(node_id)
        if failure is not None:
            self._request_metrics.failed_request_count += 1
            raise failure
        variant_type = self.variant_type_by_node_id.get(node_id)
        if variant_type == "Double" and value is not None:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError("Value does not conform to OPC UA Double")
        records = self.history_values.setdefault(node_id, [])
        records[:] = [record for record in records if record.source_timestamp != timestamp]
        records.append(data_value_dto(value, quality, timestamp))
        records.sort(key=lambda record: record.source_timestamp or _HISTORY_SORT_FALLBACK)
        self._request_metrics.history_write_count += 1

    async def write_value(
        self,
        node_id: str,
        value: Any,
        variant_type: str | None,
        quality: str,
        timestamp: datetime,
        verify_metadata: bool = True,
    ) -> None:
        del verify_metadata
        failure = self.write_failures.get(node_id)
        if failure is not None:
            self._request_metrics.failed_request_count += 1
            raise failure
        self.values[node_id] = value
        self.last_write_variant_type_by_node_id[node_id] = variant_type
        self.last_write_vqt_by_node_id[node_id] = (quality, timestamp)
        self._request_metrics.write_count += 1

    async def read_server_status_data_value(self) -> DataValueDTO:
        self.server_status_reads += 1
        return data_value_dto(
            {
                "StartTime": datetime(2026, 1, 1, 8, 11, 22, tzinfo=timezone.utc),
                "CurrentTime": datetime(2026, 1, 1, 9, 3, 47, tzinfo=timezone.utc),
                "State": "Running",
                "BuildInfo": {
                    "ProductUri": "urn:vendor:server",
                    "ManufacturerName": "Vendor",
                    "ProductName": "UA Server",
                    "SoftwareVersion": "1.2.3",
                    "BuildNumber": "20260613",
                    "BuildDate": datetime(2026, 1, 1, 7, 59, 0, tzinfo=timezone.utc),
                },
                "SecondsTillShutdown": 0,
                "ShutdownReason": "",
            },
            "Good",
            datetime(2026, 1, 1, 9, 3, 47, tzinfo=timezone.utc),
        )

    async def call_method(self, object_node_id: str, method_node_id: str, args: list[Any]) -> Any:
        return {"object": object_node_id, "method": method_node_id, "args": args}

    def get_connection_snapshot(self) -> OpcUaConnectionSnapshot:
        return self._connection_snapshot

    def snapshot_request_metrics(self) -> OpcUaRequestMetrics:
        return self._request_metrics

    def reset_runtime_metrics(self) -> None:
        self._runtime_metrics = OpcUaRuntimeMetrics()

    def snapshot_runtime_metrics(self) -> OpcUaRuntimeMetrics:
        # Mirrors the adapter: a copy, so a logged snapshot cannot mutate live counters.
        return replace(self._runtime_metrics)

    async def get_operational_limits(self) -> OpcUaOperationalLimits:
        return self._operational_limits

    async def get_subscription_capabilities(self) -> OpcUaSubscriptionCapabilities:
        return OpcUaSubscriptionCapabilities(
            max_monitored_items_per_call=1,
            max_subscriptions=100,
            max_monitored_items=100,
            max_subscriptions_per_session=100,
            max_monitored_items_per_subscription=1,
        )

    async def create_datachange_subscription(self, publishing_interval_ms: float, handler: Any) -> Any:
        del publishing_interval_ms
        self.handlers.append(handler)
        self.create_subscription_calls += 1
        return SimpleNamespace(id=f"ua-sub-{self.create_subscription_calls}", delete=self._noop_async)

    async def subscribe_data_changes(self, subscription: Any, node_ids: list[str]) -> list[int]:
        del subscription
        handles = list(range(self._next_handle, self._next_handle + len(node_ids)))
        self.subscribe_calls += len(node_ids)
        self._next_handle += len(node_ids)
        return handles

    async def unsubscribe_data_changes(self, subscription: Any, handles: list[int]) -> None:
        self.unsubscribed_calls.append((subscription, list(handles)))

    async def delete_subscription(self, subscription: Any) -> None:
        self.deleted_subscriptions.append(subscription)
        await self._noop_async()

    def get_monitored_item_handles(self, subscription: Any, node_ids: list[str]) -> dict[str, int] | None:
        del node_ids
        return self.restored_handles.get(id(subscription))

    def add_reconnect_listener(self, listener: Any) -> None:
        self.listeners.append(listener)

    def remove_reconnect_listener(self, listener: Any) -> None:
        self.removed_listeners.append(listener)
        if listener in self.listeners:
            self.listeners.remove(listener)

    async def _noop_async(self) -> None:
        return None


class SubscriptionClientFake(FakeOpcUaClient):
    """Subscription-runtime variant: generous server limits and index-based reads.

    Batching 100 monitored items per call keeps the subscription service's bin/capacity logic
    exercised; the canonical double caps at one item per call, mirroring a constrained server.
    """

    async def get_subscription_capabilities(self) -> OpcUaSubscriptionCapabilities:
        return OpcUaSubscriptionCapabilities(
            max_monitored_items_per_call=100,
            max_subscriptions=100,
            max_monitored_items=100,
            max_subscriptions_per_session=100,
            max_monitored_items_per_subscription=100,
        )

    async def read_values(self, node_ids: list[str]) -> list[float]:
        self.read_values_calls += 1
        return [float(index + self.read_values_calls) for index, _ in enumerate(node_ids)]

    async def read_data_values(self, node_ids: list[str]) -> list[DataValueDTO]:
        self.read_data_values_calls += 1
        results: list[DataValueDTO] = []
        for index, node_id in enumerate(node_ids):
            override = self.data_value_overrides.get(node_id)
            if override is not None:
                results.append(override)
                continue
            results.append(
                data_value_dto(
                    float(index + self.read_data_values_calls),
                    "Good",
                    datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                )
            )
        return results
