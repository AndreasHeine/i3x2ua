"""Browse traversal and browse-service plumbing for the OPC UA adapter.

Walks the configured browse roots with continuation-point recovery, and issues the
Browse/BrowseNext service calls in server-sized batches with reconnect retries. Node
attribute reads and reference-name resolution go through the client's entry points,
which delegate to ``node_reading.py``, so monkeypatching the client keeps working.
Tests and monkeypatching target this module.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Sequence
from time import perf_counter
from typing import TYPE_CHECKING, Any

from asyncua import ua
from asyncua.ua import NodeClass
from asyncua.ua.object_ids import ObjectIds
from asyncua.ua.uaerrors import UaStatusCodeError

from i3x_server.domain.ports.opcua import DEFAULT_MAX_NODES_PER_BROWSE, OpcUaNodeInfo
from i3x_server.infrastructure.opcua.batching import chunked

if TYPE_CHECKING:
    from asyncua.common.node import Node

    from i3x_server.infrastructure.opcua.client import OpcUaClient

logger = logging.getLogger(__name__)


async def resolve_browse_roots(client: OpcUaClient) -> list[Node]:
    if client.browse_roots is None:
        return [client.raw_client.nodes.objects]
    if not client.browse_roots:
        raise ValueError("Configured OPC UA browse roots must not be empty")
    namespace_array = await client.raw_client.get_namespace_array()
    roots: dict[str, Node] = {}
    for configured_id in client.browse_roots:
        expanded = ua.NodeId.from_string(configured_id)
        if not isinstance(expanded, ua.ExpandedNodeId) or not expanded.NamespaceUri or expanded.ServerIndex:
            raise ValueError(f"Browse root must be a local URI-based ExpandedNodeId: {configured_id!r}")
        try:
            namespace_index = namespace_array.index(expanded.NamespaceUri)
        except ValueError as exc:
            raise ValueError(f"Browse root namespace is not available on {client.endpoint}: {configured_id!r}") from exc
        node_id = ua.NodeId(expanded.Identifier, ua.Int16(namespace_index), expanded.NodeIdType)
        node = client.raw_node(node_id)
        try:
            node_class = await node.read_node_class()
        except UaStatusCodeError as exc:
            raise ValueError(f"Cannot read configured OPC UA browse root {configured_id!r}: {exc}") from exc
        if node_class != NodeClass.Object:
            raise ValueError(f"Configured OPC UA browse root is not an Object: {configured_id!r}")
        roots[node_id.to_string()] = node
    logger.info(
        "OPC UA browse roots resolved endpoint=%s configured=%s resolved=%s",
        client.endpoint,
        client.browse_roots,
        list(roots),
    )
    return list(roots.values())


async def browse_tree(client: OpcUaClient) -> list[OpcUaNodeInfo]:
    started = perf_counter()
    client.runtime_metrics.browse_tree_calls += 1
    limits = await client.get_operational_limits()
    max_nodes_per_browse = limits.max_nodes_per_browse or DEFAULT_MAX_NODES_PER_BROWSE
    logger.info(
        "OPC UA browse started endpoint=%s browse_concurrency=%d max_nodes_per_browse=%d",
        client.endpoint,
        client.browse_concurrency,
        max_nodes_per_browse,
    )
    roots = await client.resolve_browse_roots()
    output: list[OpcUaNodeInfo] = []
    visited: set[str] = set()
    reference_type_names: dict[str, str] = {}
    reference_type_inverse_names: dict[str, str] = {}
    stack: list[tuple[Any, str | None]] = [(root, None) for root in roots]

    while stack:
        batch_entries = stack[:max_nodes_per_browse]
        stack = stack[max_nodes_per_browse:]

        filtered_entries: list[tuple[Any, str | None]] = []
        for node, parent_node_id in batch_entries:
            node_id = node.nodeid.to_string()
            if node_id in visited:
                continue
            visited.add(node_id)
            filtered_entries.append((node, parent_node_id))

        if not filtered_entries:
            continue

        nodes = [node for node, _ in filtered_entries]
        node_infos, all_refs, browsed = await _gather_cancelled_on_failure(
            [
                client.read_node_infos_limited(filtered_entries),
                client.browse_references_descriptions(
                    nodes,
                    max_nodes_per_browse=max_nodes_per_browse,
                    reference_type_id=ObjectIds.References,
                ),
                client.browse_children_descriptions(nodes, max_nodes_per_browse),
            ]
        )
        output.extend(node_infos)

        await client.read_reference_type_browse_names(
            [ref for _, refs in all_refs for ref in refs],
            reference_type_names,
            reference_type_inverse_names,
        )
        node_info_by_id = {item.node_id: item for item in node_infos}
        for parent_node, refs in all_refs:
            info = node_info_by_id.get(parent_node.nodeid.to_string())
            if info is None:
                continue
            info.outgoing_references = client.to_reference_infos(
                refs, reference_type_names, reference_type_inverse_names
            )
        for parent_node, refs in browsed:
            parent_node_id = parent_node.nodeid.to_string()
            for ref in refs:
                stack.append((client.raw_node(ref.NodeId), parent_node_id))

    logger.info(
        "OPC UA browse finished endpoint=%s node_count=%d duration_s=%.3f",
        client.endpoint,
        len(output),
        perf_counter() - started,
    )
    client.runtime_metrics.browse_tree_nodes_last = len(output)
    return output


async def browse_children_descriptions(
    client: OpcUaClient,
    nodes: list[Any],
    max_nodes_per_browse: int,
) -> list[tuple[Any, list[ua.ReferenceDescription]]]:
    return await client.browse_references_descriptions(
        nodes,
        max_nodes_per_browse=max_nodes_per_browse,
        reference_type_id=ObjectIds.HierarchicalReferences,
    )


def _browse_description(
    node: Any,
    browse_direction: ua.BrowseDirection,
    reference_type_id: int,
    include_subtypes: bool,
) -> ua.BrowseDescription:
    """Browse request entry for one node, with the masks every call uses."""
    desc = ua.BrowseDescription()
    desc.NodeId = node.nodeid
    desc.BrowseDirection = browse_direction
    desc.ReferenceTypeId = ua.NodeId(ua.Int32(reference_type_id))
    desc.IncludeSubtypes = include_subtypes
    desc.NodeClassMask = ua.NodeClass.Unspecified
    desc.ResultMask = ua.BrowseResultMask.All
    return desc


def _browse_parameters(
    nodes: Sequence[Any],
    browse_direction: ua.BrowseDirection,
    reference_type_id: int,
    include_subtypes: bool,
) -> ua.BrowseParameters:
    """A Browse request covering ``nodes``: entries plus the shared request fields."""
    params = ua.BrowseParameters()
    params.View = ua.ViewDescription()
    params.RequestedMaxReferencesPerNode = 0
    params.NodesToBrowse = [
        _browse_description(node, browse_direction, reference_type_id, include_subtypes) for node in nodes
    ]
    return params


async def browse_references_descriptions(
    client: OpcUaClient,
    nodes: list[Any],
    max_nodes_per_browse: int,
    reference_type_id: int,
    browse_direction: ua.BrowseDirection = ua.BrowseDirection.Forward,
    include_subtypes: bool = True,
) -> list[tuple[Any, list[ua.ReferenceDescription]]]:
    if not nodes:
        return []

    results: list[tuple[Any, list[ua.ReferenceDescription]]] = []
    for node_batch in chunked(nodes, max_nodes_per_browse):
        params = _browse_parameters(node_batch, browse_direction, reference_type_id, include_subtypes)

        client.runtime_metrics.browse_calls += 1
        client.runtime_metrics.browse_nodes += len(node_batch)
        logger.debug(
            "OPC UA browse batch endpoint=%s nodes_to_browse=%d",
            client.endpoint,
            len(node_batch),
        )
        browse_results = await client.browse_with_retry(params=params, batch_size=len(node_batch))
        initial_references_total = sum(len(result.References) for result in browse_results)
        client.runtime_metrics.browse_initial_references += initial_references_total
        logger.debug(
            "OPC UA browse batch result endpoint=%s nodes=%d initial_references=%d",
            client.endpoint,
            len(node_batch),
            initial_references_total,
        )

        continuation_entries: list[tuple[Any, bytes, list[ua.ReferenceDescription]]] = []
        browse_next_references_total = 0
        for node, browse_result in zip(node_batch, browse_results, strict=True):
            refs = list(browse_result.References)
            continuation_point = browse_result.ContinuationPoint
            results.append((node, refs))
            if continuation_point:
                continuation_entries.append((node, continuation_point, refs))

        while continuation_entries:
            next_params = ua.BrowseNextParameters()
            next_params.ReleaseContinuationPoints = False
            next_params.ContinuationPoints = [continuation_point for _, continuation_point, _ in continuation_entries]
            try:
                next_results = await client.browse_next_with_retry(
                    next_params=next_params,
                    node_ids=[node.nodeid.to_string() for node, _, _ in continuation_entries],
                )
            except UaStatusCodeError as exc:
                if not client.is_continuation_point_invalid(exc):
                    raise
                # Session recreation invalidated the continuation points: re-browse the
                # affected nodes against the live session and continue from the fresh
                # points instead of aborting the whole tree read (L10).
                logger.warning(
                    "OPC UA browse-next continuation points stale endpoint=%s node_count=%d; re-browsing nodes",
                    client.endpoint,
                    len(continuation_entries),
                    exc_info=True,
                )
                recovery_params = _browse_parameters(
                    [node for node, _, _ in continuation_entries],
                    browse_direction,
                    reference_type_id,
                    include_subtypes,
                )
                recovery_results = await client.browse_with_retry(
                    params=recovery_params,
                    batch_size=len(continuation_entries),
                )
                if len(recovery_results) != len(continuation_entries):
                    raise RuntimeError(
                        f"Browse returned {len(recovery_results)} results for "
                        f"{len(continuation_entries)} re-browsed nodes"
                    ) from exc
                stale_entries = continuation_entries
                continuation_entries = []
                for (node, _, refs), recovery_result in zip(stale_entries, recovery_results, strict=True):
                    seen = {(item.NodeId.to_string(), item.BrowseName.to_string()) for item in refs}
                    for fresh_ref in recovery_result.References:
                        key = (fresh_ref.NodeId.to_string(), fresh_ref.BrowseName.to_string())
                        if key not in seen:
                            refs.append(fresh_ref)
                            seen.add(key)
                            browse_next_references_total += 1
                    if recovery_result.ContinuationPoint:
                        continuation_entries.append((node, recovery_result.ContinuationPoint, refs))
                    continue

            client.runtime_metrics.browse_next_calls += 1
            if len(next_results) != len(continuation_entries):
                raise RuntimeError(
                    f"BrowseNext returned {len(next_results)} results for {len(continuation_entries)} points"
                )

            next_entries: list[tuple[Any, bytes, list[ua.ReferenceDescription]]] = []
            for (node, _, refs), next_result in zip(continuation_entries, next_results, strict=True):
                refs.extend(next_result.References)
                browse_next_references_total += len(next_result.References)
                client.runtime_metrics.browse_next_references += len(next_result.References)
                if next_result.ContinuationPoint:
                    next_entries.append((node, next_result.ContinuationPoint, refs))
            continuation_entries = next_entries

        if browse_next_references_total:
            logger.debug(
                "OPC UA browse-next endpoint=%s nodes=%d additional_references=%d",
                client.endpoint,
                len(browse_results),
                browse_next_references_total,
            )

    return results


async def browse_with_retry(client: OpcUaClient, params: ua.BrowseParameters, batch_size: int) -> list[ua.BrowseResult]:
    try:
        results = await client.browse_raw(params)
        client.record_browse_results(results)
        return results
    except Exception as exc:
        if not client.should_retry_after_disconnect(exc):
            client.record_failed_request()
            raise
        logger.warning(
            "OPC UA browse retry after reconnect endpoint=%s batch_size=%d",
            client.endpoint,
            batch_size,
        )
        await client.wait_for_reconnect(exc)
        results = await client.retry_call(client.browse_raw(params))
        client.record_browse_results(results)
        return results


async def browse_next_with_retry(
    client: OpcUaClient,
    next_params: ua.BrowseNextParameters,
    node_ids: list[str],
) -> list[ua.BrowseResult]:
    try:
        results = await client.browse_next_raw(next_params)
        client.record_browse_results(results)
        return results
    except Exception as exc:
        if not client.should_retry_after_disconnect(exc):
            client.record_failed_request()
            raise
        logger.warning(
            "OPC UA browse-next retry after reconnect endpoint=%s node_count=%d",
            client.endpoint,
            len(node_ids),
        )
        await client.wait_for_reconnect(exc)
        results = await client.retry_call(client.browse_next_raw(next_params))
        client.record_browse_results(results)
        return results


async def _gather_cancelled_on_failure(awaitables: Sequence[Awaitable[Any]]) -> list[Any]:
    """Run awaitables concurrently and cancel every sibling when one of them fails.

    ``asyncio.gather`` leaves the remaining tasks running after the first failure - they keep
    issuing requests against a session that a browse or read error may already be tearing down,
    and an aborted model build can therefore still mutate connection state. Cancelling and
    awaiting the siblings before re-raising keeps the failure path deterministic (the same pattern
    ``read_data_values_per_node`` already uses for its batches).
    """
    tasks: list[asyncio.Task[Any]] = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
