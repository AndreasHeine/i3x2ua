"""Domain port contracts for OPC UA access.

Single source of truth for OPC UA Protocol interfaces and data-transfer
objects used by the application and infrastructure layers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from asyncua import ua


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


@dataclass(slots=True)
class OpcUaReferenceInfo:
    target_node_id: str
    reference_type_id: str
    reference_browse_name: str


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


@dataclass(slots=True)
class OpcUaNamespaceInfo:
    uri: str
    display_name: str


@dataclass(slots=True)
class OpcUaOperationalLimits:
    max_nodes_per_browse: int | None
    max_nodes_per_read: int | None


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


class OpcUaClientProtocol(Protocol):
    async def browse_tree(self) -> list[OpcUaNodeInfo]: ...

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

    async def read_data_values(self, node_ids: list[str]) -> list[ua.DataValue]: ...

    async def read_write_access(self, node_id: str) -> tuple[bool, bool]: ...

    async def read_variant_type(self, node_id: str) -> str | None: ...

    async def write_value(self, node_id: str, value: Any) -> None: ...

    async def read_server_status_data_value(self) -> ua.DataValue: ...

    async def read_history_values(
        self,
        node_ids: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> dict[str, list[ua.DataValue]]: ...

    async def call_method(self, object_node_id: str, method_node_id: str, args: list[Any]) -> Any: ...

    async def create_datachange_subscription(self, publishing_interval_ms: float, handler: Any) -> Any: ...

    async def subscribe_data_changes(self, subscription: Any, node_ids: list[str]) -> Any: ...

    async def delete_subscription(self, subscription: Any) -> None: ...

    def add_reconnect_listener(self, listener: Callable[[], Awaitable[None]]) -> None: ...


__all__ = [
    "OpcUaClientProtocol",
    "OpcUaConnectionSnapshot",
    "OpcUaNamespaceInfo",
    "OpcUaNodeInfo",
    "OpcUaObjectTypeInfo",
    "OpcUaObjectTypeMemberInfo",
    "OpcUaOperationalLimits",
    "OpcUaReferenceInfo",
    "OpcUaRequestMetrics",
    "OpcUaRuntimeMetrics",
    "OpcUaSubscriptionCapabilities",
]
