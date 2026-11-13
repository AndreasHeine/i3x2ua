"""Domain port contracts for OPC UA access.

Single source of truth for OPC UA Protocol interfaces and data-transfer
objects used by the application and infrastructure layers. The port stays
free of transport libraries: asyncua ``DataValue``/``Variant`` objects are
converted into the DTOs below at the adapter boundary (PR-09), and
``.importlinter`` forbids ``asyncua`` imports here permanently.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@dataclass(slots=True)
class OpcUaNodeInfo:
    node_id: str
    parent_node_id: str | None
    browse_name: str
    display_name: str
    node_class: str
    data_type: str | None
    type_definition_id: str | None = None
    event_notifier: bool = False
    outgoing_references: list[OpcUaReferenceInfo] = field(default_factory=list)
    description: str | None = None
    access_level: int | None = None
    user_access_level: int | None = None
    executable: bool | None = None
    user_executable: bool | None = None


@dataclass(frozen=True, slots=True)
class OpcUaTypeDefinitionInfo:
    node_id: str
    browse_name: str
    namespace_uri: str | None = None


@dataclass(slots=True)
class OpcUaReferenceInfo:
    target_node_id: str
    reference_type_id: str
    reference_browse_name: str
    # ``ReferenceType.InverseName`` attribute as published by the address space (OPC UA Part 3);
    # ``None`` when the server does not expose the attribute.
    reference_inverse_name: str | None = None


@dataclass(slots=True)
class OpcUaObjectTypeInfo:
    node_id: str
    parent_node_id: str | None
    browse_name: str
    display_name: str
    properties: dict[str, str | None]
    description: str | None = None
    is_abstract: bool | None = None
    members: list[OpcUaObjectTypeMemberInfo] = field(default_factory=list)


@dataclass(slots=True)
class OpcUaObjectTypeMemberInfo:
    node_id: str
    browse_name: str
    display_name: str
    description: str | None
    node_class: str
    data_type: str | None
    modelling_rule: str | None = None
    value: Any = None
    schema_value: Any = None
    variant_type: str | None = None
    is_array: bool | None = None
    value_rank: int | None = None
    array_dimensions: list[int] | None = None
    parent_node_id: str | None = None
    type_definition_id: str | None = None
    reference_type_id: str | None = None
    reference_type: str | None = None
    reference_order: int | None = None


@dataclass(slots=True)
class OpcUaNamespaceInfo:
    uri: str
    display_name: str
    namespace_version: str | None = None
    server_uri: str | None = None


@dataclass(slots=True)
class OpcUaOperationalLimits:
    max_nodes_per_browse: int | None
    max_nodes_per_read: int | None
    max_nodes_per_history_read_data: int | None = None


@dataclass(slots=True)
class OpcUaSubscriptionCapabilities:
    max_monitored_items_per_call: int | None
    max_subscriptions: int | None
    max_monitored_items: int | None
    max_subscriptions_per_session: int | None
    max_monitored_items_per_subscription: int | None


@dataclass(slots=True)
class OpcUaRuntimeMetrics:
    browse_calls: int = 0
    browse_nodes: int = 0
    browse_initial_references: int = 0
    browse_next_calls: int = 0
    browse_next_references: int = 0
    read_calls: int = 0
    read_nodes: int = 0
    history_read_calls: int = 0
    history_read_nodes: int = 0
    namespace_reads: int = 0
    namespace_count_last: int = 0
    namespace_info_builds: int = 0
    namespace_info_count_last: int = 0
    object_type_reads: int = 0
    object_type_count_last: int = 0
    browse_tree_calls: int = 0
    browse_tree_nodes_last: int = 0
    method_calls: int = 0


@dataclass(slots=True)
class OpcUaRequestMetrics:
    read_count: int = 0
    write_count: int = 0
    browse_count: int = 0
    method_call_count: int = 0
    history_read_count: int = 0
    history_write_count: int = 0
    failed_request_count: int = 0
    goodish_qualities: list[str] = field(default_factory=lambda: ["Good", "Uncertain"])


@dataclass(slots=True)
class OpcUaConnectionSnapshot:
    state: str
    endpoint: str
    since: datetime


@dataclass(frozen=True, slots=True)
class OpcUaValue:
    """Decoded OPC UA variant payload as delivered across the adapter boundary.

    ``value`` is the variant payload in Python-native form (``None`` for a Null
    variant); ``variant_type`` is the variant type name (``"Double"`` ...) when the
    adapter could identify it, and ``is_array`` marks array payloads.
    """

    value: Any
    variant_type: str | None = None
    is_array: bool | None = None


@dataclass(frozen=True, slots=True)
class DataValueDTO:
    """A value/quality/timestamp triple (VQT) in domain form.

    ``quality`` is the adapter-decoded signal (``Good``/``Uncertain``/``Bad``/
    ``GoodNoData``; ``None`` when the status code carries no signal), and
    ``status_code`` is the numeric OPC UA status code so callers can apply their
    own severity mapping. ``source_timestamp`` is preferred over
    ``server_timestamp`` when both are present.
    """

    value: OpcUaValue
    status_code: int | None = None
    quality: str | None = None
    source_timestamp: datetime | None = None
    server_timestamp: datetime | None = None


class OpcUaDataChangeHandler(Protocol):
    """Callback for datachange notifications handed over by the adapter.

    ``node_id`` is the monitored node as a string, ``value`` the notification
    payload, and ``data_value`` the notification's VQT (``None`` when the
    notification carries no monitored-item value).
    """

    def datachange_notification(self, node_id: str, value: Any, data_value: DataValueDTO | None) -> None: ...

    def event_notification(self, event: Any) -> None: ...


@runtime_checkable
class OpcUaSubscriptionHandle(Protocol):
    """Opaque handle for a native OPC UA subscription.

    The adapter owns the handle's shape; callers only pass it back to the
    subscription methods of :class:`OpcUaClientProtocol`. ``runtime_checkable``
    documents the structural guarantee: an empty protocol matches any object.
    """


class OpcUaWriteMetadataUnsupportedError(Exception):
    """Raised when an OPC UA server does not preserve current-value VQT metadata."""


class OpcUaWriteMetadataVerificationError(Exception):
    """Raised when current-value VQT metadata cannot be verified after writing."""


# Verification messages shared by the adapter (per-write verification) and the bulk write
# path (batched read-back), so both attribute the same detail text to failed items.
WRITE_METADATA_UNCERTAIN_MESSAGE = (
    "OPC UA server accepted the current-value write, but the requested quality and source "
    "timestamp could not be verified; write outcome is uncertain"
)
WRITE_METADATA_UNSUPPORTED_MESSAGE = (
    "OPC UA server did not preserve the requested quality and source timestamp; "
    "current-value VQT writes are unsupported by this server"
)


class OpcUaClientProtocol(Protocol):
    async def browse_tree(self) -> list[OpcUaNodeInfo]: ...

    async def resolve_type_definition_hierarchy(self, type_definition_id: str) -> list[OpcUaTypeDefinitionInfo]: ...

    async def resolve_type_definition_hierarchies(
        self, type_definition_ids: list[str]
    ) -> dict[str, list[OpcUaTypeDefinitionInfo]]: ...

    async def get_namespaces(self) -> list[str]: ...

    async def get_namespace_infos(self) -> list[OpcUaNamespaceInfo]: ...

    async def get_object_types(self) -> list[OpcUaObjectTypeInfo]: ...

    async def get_operational_limits(self) -> OpcUaOperationalLimits: ...

    async def get_subscription_capabilities(self) -> OpcUaSubscriptionCapabilities: ...

    def get_connection_snapshot(self) -> OpcUaConnectionSnapshot: ...

    def snapshot_request_metrics(self) -> OpcUaRequestMetrics: ...

    async def read_value(self, node_id: str) -> Any: ...

    async def read_browse_name(self, node_id: str) -> str | None: ...

    async def read_values(self, node_ids: list[str]) -> list[Any]: ...

    async def read_data_values(self, node_ids: list[str]) -> list[DataValueDTO]: ...

    async def read_write_access(self, node_id: str) -> tuple[bool, bool]: ...

    async def read_history_write_access(self, node_id: str) -> tuple[bool, bool]: ...

    def history_update_supported(self) -> bool | None: ...

    async def read_variant_type(self, node_id: str) -> str | None: ...

    async def write_value(
        self,
        node_id: str,
        value: Any,
        variant_type: str | None,
        quality: str,
        timestamp: datetime,
        verify_metadata: bool = True,
    ) -> None: ...

    async def read_server_status_data_value(self) -> DataValueDTO: ...

    async def read_history_values(
        self,
        node_ids: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> dict[str, list[DataValueDTO]]: ...

    async def write_history_value(self, node_id: str, value: Any, quality: str, timestamp: datetime) -> None: ...

    async def call_method(self, object_node_id: str, method_node_id: str, args: list[Any]) -> Any: ...

    async def create_datachange_subscription(
        self, publishing_interval_ms: float, handler: OpcUaDataChangeHandler
    ) -> OpcUaSubscriptionHandle: ...

    async def subscribe_data_changes(self, subscription: OpcUaSubscriptionHandle, node_ids: list[str]) -> list[int]: ...

    async def unsubscribe_data_changes(self, subscription: OpcUaSubscriptionHandle, handles: list[int]) -> None: ...

    async def delete_subscription(self, subscription: OpcUaSubscriptionHandle) -> None: ...

    def get_monitored_item_handles(
        self, subscription: OpcUaSubscriptionHandle, node_ids: list[str]
    ) -> dict[str, int] | None: ...

    def add_reconnect_listener(self, listener: Callable[[], Awaitable[None]]) -> None: ...

    def remove_reconnect_listener(self, listener: Callable[[], Awaitable[None]]) -> None: ...


__all__ = [
    "DataValueDTO",
    "OpcUaClientProtocol",
    "OpcUaConnectionSnapshot",
    "OpcUaDataChangeHandler",
    "OpcUaNamespaceInfo",
    "OpcUaNodeInfo",
    "OpcUaObjectTypeInfo",
    "OpcUaObjectTypeMemberInfo",
    "OpcUaOperationalLimits",
    "OpcUaReferenceInfo",
    "OpcUaRequestMetrics",
    "OpcUaRuntimeMetrics",
    "OpcUaSubscriptionCapabilities",
    "OpcUaSubscriptionHandle",
    "OpcUaTypeDefinitionInfo",
    "OpcUaValue",
]
