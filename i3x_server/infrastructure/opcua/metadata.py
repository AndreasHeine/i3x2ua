"""Server metadata loading: operational limits, subscription capabilities, namespaces.

The loaders read the server's limit and capability nodes and build namespace infos from
the namespace metadata components, falling back to defaults or fallback names when a
server exposes less. Cache lookup, TTL handling and coalescing stay on ``OpcUaClient``
(``client.py``); these functions are the loaders those cache entry points delegate to.
Tests and monkeypatching target this module.
"""

from __future__ import annotations

import asyncio
import logging
from time import perf_counter
from typing import TYPE_CHECKING, Any

from asyncua.ua.attribute_ids import AttributeIds
from asyncua.ua.object_ids import ObjectIds

from i3x_server.domain.ports.opcua import (
    DEFAULT_MAX_NODES_PER_BROWSE,
    OpcUaNamespaceInfo,
    OpcUaOperationalLimits,
    OpcUaSubscriptionCapabilities,
)
from i3x_server.infrastructure.opcua.browse import _gather_cancelled_on_failure

if TYPE_CHECKING:
    from i3x_server.infrastructure.opcua.client import OpcUaClient

logger = logging.getLogger(__name__)


async def _load_operational_limits(client: OpcUaClient) -> OpcUaOperationalLimits:
    started = perf_counter()
    max_nodes_per_browse: int | None = None
    max_nodes_per_read: int | None = None
    max_nodes_per_history_read_data: int | None = None

    try:
        operational_limits_node = client.raw_node("i=11704")
        children = await operational_limits_node.get_children()
        browse_names = await client.read_attribute_batch_limited(
            children,
            AttributeIds.BrowseName,
            batch_size=max(1, len(children)),
        )
        limit_nodes: list[tuple[Any, str]] = []
        supported_limits = {
            "MaxNodesPerBrowse",
            "MaxNodesPerRead",
            "MaxNodesPerHistoryReadData",
        }
        for child, browse_name_value in zip(children, browse_names, strict=True):
            browse_name = getattr(client.extract_attribute_value(browse_name_value), "Name", None)
            if browse_name in supported_limits:
                limit_nodes.append((child, browse_name))
        limit_values = await client.read_attribute_pairs(
            [(node, AttributeIds.Value) for node, _ in limit_nodes],
            batch_size=max(1, len(limit_nodes)),
        )
        for (_, browse_name), value_data in zip(limit_nodes, limit_values, strict=True):
            value = client.extract_attribute_value(value_data)
            if not isinstance(value, int) or value <= 0:
                continue
            if browse_name == "MaxNodesPerBrowse":
                max_nodes_per_browse = value
            elif browse_name == "MaxNodesPerRead":
                max_nodes_per_read = value
            elif browse_name == "MaxNodesPerHistoryReadData":
                max_nodes_per_history_read_data = value
    except Exception:
        logger.warning(
            "OPC UA operational limits read failed endpoint=%s; using internal defaults",
            client.endpoint,
            exc_info=True,
        )

    limits = OpcUaOperationalLimits(
        max_nodes_per_browse=max_nodes_per_browse,
        max_nodes_per_read=max_nodes_per_read,
        max_nodes_per_history_read_data=max_nodes_per_history_read_data,
    )
    client.limits_cache = limits
    logger.info(
        "OPC UA operational limits resolved endpoint=%s "
        "max_nodes_per_browse=%s max_nodes_per_read=%s max_nodes_per_history_read_data=%s duration_s=%.3f",
        client.endpoint,
        limits.max_nodes_per_browse,
        limits.max_nodes_per_read,
        limits.max_nodes_per_history_read_data,
        perf_counter() - started,
    )
    return limits


async def _load_subscription_capabilities(client: OpcUaClient) -> OpcUaSubscriptionCapabilities:
    started = perf_counter()

    node_ids = ["i=11714", "i=24096", "i=24097", "i=24098", "i=24104"]
    nodes = [client.raw_node(node_id) for node_id in node_ids]
    try:
        capability_values = await client.read_attribute_pairs([(node, AttributeIds.Value) for node in nodes])
    except Exception:
        logger.debug("OPC UA subscription capability batch read failed endpoint=%s", client.endpoint, exc_info=True)
        fallback_values = await _gather_cancelled_on_failure(
            [client.read_positive_int(node_id) for node_id in node_ids]
        )
        values_by_node_id = dict(zip(node_ids, fallback_values, strict=True))
    else:
        values_by_node_id = {}
        for node, data_value in zip(nodes, capability_values, strict=True):
            value = client.extract_attribute_value(data_value)
            node_id = node.nodeid.to_string()
            values_by_node_id[node_id] = value if isinstance(value, int) and value > 0 else None

    capabilities = OpcUaSubscriptionCapabilities(
        max_monitored_items_per_call=values_by_node_id.get("i=11714"),
        max_subscriptions=values_by_node_id.get("i=24096"),
        max_monitored_items=values_by_node_id.get("i=24097"),
        max_subscriptions_per_session=values_by_node_id.get("i=24098"),
        max_monitored_items_per_subscription=values_by_node_id.get("i=24104"),
    )

    client.subscription_caps_cache = capabilities
    logger.info(
        "OPC UA subscription capabilities resolved endpoint=%s max_items_per_call=%s "
        "max_subscriptions=%s max_monitored_items=%s max_subscriptions_per_session=%s "
        "max_items_per_subscription=%s duration_s=%.3f",
        client.endpoint,
        capabilities.max_monitored_items_per_call,
        capabilities.max_subscriptions,
        capabilities.max_monitored_items,
        capabilities.max_subscriptions_per_session,
        capabilities.max_monitored_items_per_subscription,
        perf_counter() - started,
    )
    return capabilities


async def read_positive_int(client: OpcUaClient, node_id: str) -> int | None:
    try:
        value = await client.raw_node(node_id).read_value()
    except Exception:
        logger.debug("OPC UA subscription capability read failed node_id=%s", node_id, exc_info=True)
        return None

    if isinstance(value, int) and value > 0:
        return value
    return None


async def _get_namespaces(client: OpcUaClient) -> list[str]:
    started = perf_counter()
    client.runtime_metrics.namespace_reads += 1
    try:
        raw = await client.raw_client.nodes.namespace_array.read_value()
        namespaces = [str(item) for item in raw] if isinstance(raw, list) else []
        client.runtime_metrics.namespace_count_last = len(namespaces)
        logger.debug(
            "OPC UA namespaces read ok endpoint=%s count=%d duration_s=%.3f",
            client.endpoint,
            len(namespaces),
            perf_counter() - started,
        )
        return namespaces
    except Exception:
        logger.exception(
            "OPC UA namespaces read failed endpoint=%s duration_s=%.3f",
            client.endpoint,
            perf_counter() - started,
        )
        raise


async def _load_namespace_infos(client: OpcUaClient) -> list[OpcUaNamespaceInfo]:
    now = perf_counter()
    if client.namespace_infos_cache is not None:
        cached_at, cached_value = client.namespace_infos_cache
        if client.is_metadata_cache_entry_fresh(cached_at):
            logger.debug(
                "OPC UA namespace info cache hit endpoint=%s age_s=%.3f",
                client.endpoint,
                now - cached_at,
            )
            return cached_value

    started = perf_counter()
    cold_build = client.runtime_metrics.namespace_info_builds == 0
    uris = await client.get_namespaces()
    server_uri: str | None = None
    try:
        server_array_node = client.raw_node(f"i={ObjectIds.Server_ServerArray}")
        server_array = await server_array_node.read_value()
        if isinstance(server_array, (list, tuple)) and server_array:
            candidate = server_array[0]
            if isinstance(candidate, str) and candidate.strip():
                server_uri = candidate.strip()
    except Exception:
        logger.warning(
            "OPC UA ServerArray read failed endpoint=%s; server URI unavailable",
            client.endpoint,
            exc_info=True,
        )

    display_by_uri: dict[str, str] = {}
    uri_by_component_id: dict[str, str] = {}
    version_by_component_id: dict[str, str] = {}

    try:
        limits = await client.get_operational_limits()
        max_nodes_per_browse = limits.max_nodes_per_browse or DEFAULT_MAX_NODES_PER_BROWSE
        namespaces_node = client.raw_node("i=11715")
        namespace_components_browse = await client.browse_children_descriptions(
            [namespaces_node],
            max_nodes_per_browse,
        )
        namespace_components = [client.raw_node(ref.NodeId) for _, refs in namespace_components_browse for ref in refs]
        logger.debug(
            "OPC UA namespace metadata components endpoint=%s count=%d",
            client.endpoint,
            len(namespace_components),
        )

        component_display_names = await asyncio.gather(
            *[component.read_display_name() for component in namespace_components],
            return_exceptions=True,
        )
        component_display_by_id: dict[str, str] = {}
        for component, display_name in zip(namespace_components, component_display_names, strict=True):
            if isinstance(display_name, BaseException):
                logger.debug(
                    "OPC UA namespace metadata display name read failed endpoint=%s node_id=%s",
                    client.endpoint,
                    component.nodeid.to_string(),
                    exc_info=display_name,
                )
                continue
            display_text = getattr(display_name, "Text", None)
            component_display_by_id[component.nodeid.to_string()] = (
                display_text if isinstance(display_text, str) else ""
            )

        component_children_browse = await client.browse_children_descriptions(
            namespace_components,
            max_nodes_per_browse,
        )

        child_entries: list[tuple[Any, str, str, str]] = []

        for component, refs in component_children_browse:
            component_display = component_display_by_id.get(component.nodeid.to_string(), "")
            logger.debug(
                "OPC UA namespace metadata component endpoint=%s node_id=%s children=%d",
                client.endpoint,
                component.nodeid.to_string(),
                len(refs),
            )
            for ref in refs:
                if ref.BrowseName.Name in {"NamespaceUri", "NamespaceVersion"}:
                    child_entries.append(
                        (
                            client.raw_node(ref.NodeId),
                            component.nodeid.to_string(),
                            ref.BrowseName.Name,
                            component_display,
                        )
                    )

        metadata_failures = 0

        def record_child_value(
            child_node: Any, component_id: str, browse_name: str, component_display: str, value: Any
        ) -> None:
            nonlocal metadata_failures
            if value is None:
                metadata_failures += 1
                logger.debug(
                    "OPC UA namespace metadata NamespaceUri read failed endpoint=%s node_id=%s",
                    client.endpoint,
                    child_node.nodeid.to_string(),
                )
                return
            if browse_name == "NamespaceUri":
                uri = str(value)
                if uri:
                    display_by_uri[uri] = component_display or uri
                    uri_by_component_id[component_id] = uri
            elif isinstance(value, str) and value.strip():
                version_by_component_id[component_id] = value.strip()

        batch_values: list[Any] | None = None
        if child_entries:
            try:
                batch_values = await client.read_attribute_pairs(
                    [(child, AttributeIds.Value) for child, _component_id, _name, _display in child_entries]
                )
            except Exception:
                logger.debug(
                    "OPC UA namespace metadata value batch read failed endpoint=%s; falling back to per-node reads",
                    client.endpoint,
                    exc_info=True,
                )
                batch_values = None

        if batch_values is not None:
            for index, (child, component_id, browse_name, component_display) in enumerate(child_entries):
                extracted = client.extract_attribute_value(batch_values[index])
                record_child_value(child, component_id, browse_name, component_display, extracted)
        else:
            for child, component_id, browse_name, component_display in child_entries:
                try:
                    value = await child.read_value()
                except Exception as exc:
                    metadata_failures += 1
                    logger.debug(
                        "OPC UA namespace metadata NamespaceUri read failed endpoint=%s node_id=%s",
                        client.endpoint,
                        child.nodeid.to_string(),
                        exc_info=exc,
                    )
                    continue
                record_child_value(child, component_id, browse_name, component_display, value)
        if metadata_failures:
            logger.warning(
                "OPC UA namespace metadata partially read endpoint=%s failures=%d; "
                "using fallback names for missing entries",
                client.endpoint,
                metadata_failures,
            )
    except Exception:
        logger.warning(
            "OPC UA namespace metadata read from i=11715 failed endpoint=%s; using fallback names",
            client.endpoint,
            exc_info=True,
        )

    resolved_versions = {
        uri: version_by_component_id[component_id]
        for component_id, uri in uri_by_component_id.items()
        if component_id in version_by_component_id
    }

    infos = [
        OpcUaNamespaceInfo(
            uri=uri,
            display_name=display_by_uri.get(uri, ""),
            namespace_version=resolved_versions.get(uri),
            server_uri=server_uri,
        )
        for uri in uris
    ]
    client.namespace_infos_cache = (perf_counter(), infos)
    client.runtime_metrics.namespace_info_builds += 1
    client.runtime_metrics.namespace_info_count_last = len(infos)
    logger.info(
        "OPC UA namespace infos built endpoint=%s count=%d duration_s=%.3f cold_build=%s",
        client.endpoint,
        len(infos),
        perf_counter() - started,
        cold_build,
    )
    return infos
