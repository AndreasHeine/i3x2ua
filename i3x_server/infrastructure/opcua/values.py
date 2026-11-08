"""Value reads, writes, history and method calls against the OPC UA server.

Reads values and data values in server-sized batches (per-node fallback for batch
failures, no fan-out for transport failures), writes current values and history with
preserved quality/timestamp metadata, reads raw history with continuation points, and
invokes methods. Reconnect waits and error classification go through the client's entry
points (``recovery.py``); metrics recording stays on the client. Tests and monkeypatching
target this module.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from time import perf_counter
from typing import TYPE_CHECKING, Any

from asyncua import ua
from asyncua.ua.attribute_ids import AttributeIds
from asyncua.ua.status_codes import StatusCodes
from asyncua.ua.uaerrors import UaError, UaStatusCodeError

from i3x_server.domain.ports.opcua import (
    OpcUaWriteMetadataUnsupportedError,
    OpcUaWriteMetadataVerificationError,
)
from i3x_server.infrastructure.opcua.browse import _chunked, _gather_cancelled_on_failure
from i3x_server.infrastructure.opcua.history import historical_data_value, quality_status_code
from i3x_server.schemas.opcua_value import quality_from_status_code

if TYPE_CHECKING:
    from i3x_server.infrastructure.opcua.client import OpcUaClient

logger = logging.getLogger(__name__)


def _to_explicit_ua_variant(value: Any, variant_type: str | None) -> ua.Variant | None:
    if value is None:
        return ua.Variant(None, ua.VariantType.Null)
    if variant_type is None:
        return None
    normalized = variant_type.strip()
    if not normalized:
        return None
    ua_variant_type = getattr(ua.VariantType, normalized, None)
    if ua_variant_type is None:
        return None
    return ua.Variant(value, ua_variant_type)


def _quality_from_status_code(status_code: ua.StatusCode | None) -> str | None:
    return quality_from_status_code(status_code)


def _attribute_scalar_value(data_value: Any) -> Any:
    if data_value is None:
        return None
    variant = getattr(data_value, "Value", None)
    if variant is None:
        return data_value
    return getattr(variant, "Value", variant)


def _is_write_access_allowed(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, set):
        labels = {str(item).lower() for item in value}
        return "currentwrite" in labels or "write" in labels
    raw = getattr(value, "value", value)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        return bool(raw & 0x02)
    try:
        numeric = int(raw)
    except (TypeError, ValueError):
        return "write" in str(raw).lower()
    return bool(numeric & 0x02)


async def read_value(client: OpcUaClient, node_id: str) -> Any:
    started = perf_counter()
    client._runtime_metrics.read_calls += 1
    client._runtime_metrics.read_nodes += 1
    node = client._client.get_node(node_id)
    try:
        value = await node.read_value()
        client._record_read_success()
        logger.debug("OPC UA read ok node_id=%s duration_s=%.3f", node_id, perf_counter() - started)
        return value
    except Exception as exc:
        if client._should_retry_after_disconnect(exc):
            logger.warning(
                "OPC UA read retry after reconnect node_id=%s endpoint=%s",
                node_id,
                client._endpoint,
            )
            await client._wait_for_reconnect(exc)
            value = await client._retry_call(client._client.get_node(node_id).read_value())
            client._record_read_success()
            logger.debug(
                "OPC UA read ok after reconnect node_id=%s duration_s=%.3f",
                node_id,
                perf_counter() - started,
            )
            return value
        client._record_failed_request()
        logger.exception("OPC UA read failed node_id=%s duration_s=%.3f", node_id, perf_counter() - started)
        raise


async def read_browse_name(client: OpcUaClient, node_id: str) -> str | None:
    started = perf_counter()
    node = client._client.get_node(node_id)
    try:
        browse_name = await node.read_browse_name()
        resolved = getattr(browse_name, "Name", None)
        if isinstance(resolved, str) and resolved:
            return resolved
        return str(browse_name) if browse_name is not None else None
    except Exception as exc:
        if client._should_retry_after_disconnect(exc):
            logger.warning(
                "OPC UA browse-name retry after reconnect node_id=%s endpoint=%s",
                node_id,
                client._endpoint,
            )
            await client._wait_for_reconnect(exc)
            retry_name = await client._retry_call(client._client.get_node(node_id).read_browse_name())
            resolved_retry = getattr(retry_name, "Name", None)
            if isinstance(resolved_retry, str) and resolved_retry:
                return resolved_retry
            return str(retry_name) if retry_name is not None else None
        logger.debug(
            "OPC UA browse-name read failed node_id=%s duration_s=%.3f",
            node_id,
            perf_counter() - started,
            exc_info=True,
        )
        return None


async def read_values(client: OpcUaClient, node_ids: list[str]) -> list[Any]:
    if not node_ids:
        return []

    started = perf_counter()
    limits = await client.get_operational_limits()
    max_nodes = limits.max_nodes_per_read or len(node_ids)
    batch_size = max(1, min(max_nodes, len(node_ids)))

    values: list[Any] = []
    for batch in _chunked(node_ids, batch_size):
        values.extend(await client._read_values_batch_with_fallback(batch))

    logger.debug(
        "OPC UA batch read ok endpoint=%s requested=%d batch_size=%d duration_s=%.3f",
        client._endpoint,
        len(node_ids),
        batch_size,
        perf_counter() - started,
    )
    return values


async def read_data_values(client: OpcUaClient, node_ids: list[str]) -> list[ua.DataValue]:
    """Read Value attributes with one OPC UA Read service call per MaxNodesPerRead chunk.

    Per-node status codes are returned as-is. Transport failures propagate without
    per-node fan-out; other batch failures fall back to isolated per-node reads.
    """
    if not node_ids:
        return []

    started = perf_counter()
    try:
        nodes = [client._client.get_node(node_id) for node_id in node_ids]
        values = await client._read_attribute_batch_limited(nodes, AttributeIds.Value)
        if len(values) != len(node_ids):
            raise RuntimeError(f"Read returned {len(values)} results for {len(node_ids)} nodes")
    except Exception as exc:
        if client._is_transport_read_failure(exc):
            logger.warning(
                "OPC UA batched data-value read unavailable endpoint=%s requested=%d; "
                "not falling back to per-node reads",
                client._endpoint,
                len(node_ids),
                exc_info=True,
            )
            raise
        logger.warning(
            "OPC UA batched data-value read failed endpoint=%s requested=%d; falling back to per-node reads",
            client._endpoint,
            len(node_ids),
            exc_info=True,
        )
        return await client._read_data_values_per_node(node_ids)

    logger.debug(
        "OPC UA batch data-value read ok endpoint=%s requested=%d duration_s=%.3f",
        client._endpoint,
        len(node_ids),
        perf_counter() - started,
    )
    return values


async def _read_data_values_per_node(client: OpcUaClient, node_ids: list[str]) -> list[ua.DataValue]:
    started = perf_counter()
    limits = await client.get_operational_limits()
    max_nodes = limits.max_nodes_per_read or len(node_ids)
    batch_size = max(1, min(max_nodes, len(node_ids)))
    semaphore = asyncio.Semaphore(min(batch_size, client._browse_concurrency))

    async def _read_one(node_id: str) -> ua.DataValue:
        async with semaphore:
            node = client._client.get_node(node_id)
            try:
                value = await node.read_data_value()
                client._record_read_data_values([value])
                return value
            except Exception as exc:
                if client._should_retry_after_disconnect(exc):
                    logger.warning(
                        "OPC UA data-value read retry after reconnect node_id=%s endpoint=%s",
                        node_id,
                        client._endpoint,
                    )
                    await client._wait_for_reconnect(exc)
                    try:
                        retry_value = await client._retry_call(client._client.get_node(node_id).read_data_value())
                        client._record_read_data_values([retry_value])
                        return retry_value
                    except Exception as retry_exc:
                        if client._is_transport_read_failure(retry_exc):
                            logger.warning(
                                "OPC UA data-value retry unavailable node_id=%s; aborting remaining reads",
                                node_id,
                                exc_info=True,
                            )
                            raise
                elif client._is_transport_read_failure(exc):
                    client._record_failed_request()
                    logger.warning(
                        "OPC UA data-value read unavailable node_id=%s; aborting remaining reads",
                        node_id,
                        exc_info=True,
                    )
                    raise
                logger.warning(
                    "OPC UA data-value read failed node_id=%s; returning Bad null",
                    node_id,
                    exc_info=True,
                )
                dv = ua.DataValue()
                dv.StatusCode = ua.StatusCode(ua.UInt32(0x80010000))
                client._record_read_data_values([dv])
                return dv

    results: list[ua.DataValue] = []
    for chunk in _chunked(node_ids, batch_size):
        tasks = [asyncio.create_task(_read_one(nid)) for nid in chunk]
        try:
            chunk_results = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        results.extend(chunk_results)

    client._runtime_metrics.read_calls += 1
    client._runtime_metrics.read_nodes += len(node_ids)
    logger.debug(
        "OPC UA batch data-value read ok endpoint=%s requested=%d duration_s=%.3f",
        client._endpoint,
        len(node_ids),
        perf_counter() - started,
    )
    return results


async def _read_values_batch_with_fallback(client: OpcUaClient, node_ids: list[str]) -> list[Any]:
    if not node_ids:
        return []

    client._runtime_metrics.read_calls += 1
    client._runtime_metrics.read_nodes += len(node_ids)
    nodes = [client._client.get_node(node_id) for node_id in node_ids]
    try:
        values = list(await client._client.read_values(nodes))
        client._record_read_success()
        return values
    except Exception as exc:
        candidate_error = exc
        if client._should_retry_after_disconnect(exc):
            logger.warning(
                "OPC UA batch read retry after reconnect endpoint=%s batch_size=%d",
                client._endpoint,
                len(node_ids),
            )
            await client._wait_for_reconnect(exc)
            retry_nodes = [client._client.get_node(node_id) for node_id in node_ids]
            try:
                values = list(await client._client.read_values(retry_nodes))
                client._record_read_success()
                return values
            except Exception as retry_exc:
                candidate_error = retry_exc

        if len(node_ids) <= 1:
            client._record_failed_request()
            logger.warning(
                "OPC UA single-node read failed endpoint=%s node_id=%s error=%s; returning null value",
                client._endpoint,
                node_ids[0],
                candidate_error,
            )
            return [None]

        split_at = max(1, len(node_ids) // 2)
        logger.warning(
            "OPC UA batch read split fallback endpoint=%s batch_size=%d split_at=%d error=%s",
            client._endpoint,
            len(node_ids),
            split_at,
            candidate_error,
        )
        left = await client._read_values_batch_with_fallback(node_ids[:split_at])
        right = await client._read_values_batch_with_fallback(node_ids[split_at:])
        return left + right


async def read_write_access(client: OpcUaClient, node_id: str) -> tuple[bool, bool]:
    node = client._client.get_node(node_id)
    try:
        values = await node.read_attributes([AttributeIds.AccessLevel, AttributeIds.UserAccessLevel])
        access_level = _attribute_scalar_value(values[0]) if len(values) > 0 else None
        user_access_level = _attribute_scalar_value(values[1]) if len(values) > 1 else None
        return _is_write_access_allowed(access_level), _is_write_access_allowed(user_access_level)
    except Exception as exc:
        if client._should_retry_after_disconnect(exc):
            logger.warning(
                "OPC UA write access read retry after reconnect node_id=%s endpoint=%s",
                node_id,
                client._endpoint,
            )
            await client._wait_for_reconnect(exc)
            retry_node = client._client.get_node(node_id)
            retry_values = await client._retry_call(
                retry_node.read_attributes([AttributeIds.AccessLevel, AttributeIds.UserAccessLevel])
            )
            access_level = _attribute_scalar_value(retry_values[0]) if len(retry_values) > 0 else None
            user_access_level = _attribute_scalar_value(retry_values[1]) if len(retry_values) > 1 else None
            return _is_write_access_allowed(access_level), _is_write_access_allowed(user_access_level)
        client._record_failed_request()
        raise


async def read_history_write_access(client: OpcUaClient, node_id: str) -> tuple[bool, bool]:
    node = client._client.get_node(node_id)
    try:
        values = await node.read_attributes([AttributeIds.AccessLevel, AttributeIds.UserAccessLevel])
        if len(values) != 2:
            raise UaError("Incomplete OPC UA history access response")
        for item in values:
            if item.StatusCode is None:
                raise UaError("Missing OPC UA history access status")
            item.StatusCode.check()
        mask = 1 << ua.AccessLevel.HistoryWrite
        return (
            bool(int(_attribute_scalar_value(values[0])) & mask),
            bool(int(_attribute_scalar_value(values[1])) & mask),
        )
    except Exception:
        client._record_failed_request()
        raise


async def write_history_value(client: OpcUaClient, node_id: str, value: Any, quality: str, timestamp: datetime) -> None:
    node = client._client.get_node(node_id)
    try:
        variant_type = await node.read_data_type_as_variant_type()
        attributes = await node.read_attributes([AttributeIds.ValueRank, AttributeIds.ArrayDimensions])
        if len(attributes) != 2:
            raise UaError("Incomplete OPC UA history type response")
        for attribute in attributes:
            if attribute.StatusCode is None:
                raise UaError("Missing OPC UA history type status")
        rank_attribute, dimensions_attribute = attributes
        assert rank_attribute.StatusCode is not None
        assert dimensions_attribute.StatusCode is not None
        rank_attribute.StatusCode.check()
        dimensions = None
        if dimensions_attribute.StatusCode.value != StatusCodes.BadAttributeIdInvalid:
            dimensions_attribute.StatusCode.check()
            dimensions = _attribute_scalar_value(dimensions_attribute)
        data_value = historical_data_value(
            value,
            quality,
            timestamp,
            variant_type,
            int(_attribute_scalar_value(rank_attribute)),
            dimensions,
        )
        details = ua.UpdateDataDetails(
            NodeId=node.nodeid,
            PerformInsertReplace=ua.PerformUpdateType.Update,
            UpdateValues=[data_value],
        )
        # Do not replay a mutation after a timeout: the server may already have applied it.
        result = await node.history_update(details)
        result.StatusCode.check()
        if len(result.OperationResults) != 1:
            raise UaError("Incomplete OPC UA HistoryUpdate operation results")
        result.OperationResults[0].check()
    except Exception as exc:
        if isinstance(exc, UaStatusCodeError) and exc.code == StatusCodes.BadServiceUnsupported:
            client._history_update_supported = False
        client._record_failed_request()
        raise
    client._history_update_supported = True
    client._request_metrics.history_write_count += 1
    logger.info("OPC UA history update ok node_id=%s timestamp=%s", node_id, timestamp.isoformat())


async def read_variant_type(client: OpcUaClient, node_id: str) -> str | None:
    node = client._client.get_node(node_id)
    try:
        variant_type = await node.read_data_type_as_variant_type()
        return str(getattr(variant_type, "name", variant_type)) if variant_type is not None else None
    except Exception as exc:
        if client._should_retry_after_disconnect(exc):
            logger.warning(
                "OPC UA variant type read retry after reconnect node_id=%s endpoint=%s",
                node_id,
                client._endpoint,
            )
            await client._wait_for_reconnect(exc)
            retry_node = client._client.get_node(node_id)
            retry_variant_type = await client._retry_call(retry_node.read_data_type_as_variant_type())
            return (
                str(getattr(retry_variant_type, "name", retry_variant_type)) if retry_variant_type is not None else None
            )
        return None


async def write_value(
    client: OpcUaClient,
    node_id: str,
    value: Any,
    variant_type: str | None,
    quality: str,
    timestamp: datetime,
) -> None:
    if timestamp.tzinfo is None:
        # A naive timestamp would be reinterpreted in the host timezone by the read-back
        # comparison below; the historical write path rejects it the same way (`history.py`).
        raise ValueError("Current-value timestamp must include a timezone")
    node = client._client.get_node(node_id)
    explicit_variant = _to_explicit_ua_variant(value, variant_type)
    payload = ua.DataValue(
        Value=explicit_variant if explicit_variant is not None else ua.Variant(value),
        StatusCode=quality_status_code(quality),
        SourceTimestamp=ua.DateTime.fromisoformat(timestamp.astimezone(timezone.utc).isoformat()),
    )
    try:
        # Do not replay a write after a connection loss: the server may already have applied it.
        await node.write_value(payload)
        client._request_metrics.write_count += 1
    except Exception:
        client._record_failed_request()
        raise

    try:
        written_data_value = await node.read_data_value(raise_on_bad_status=False)
    except Exception as exc:
        client._record_failed_request()
        raise OpcUaWriteMetadataVerificationError(
            "OPC UA server accepted the current-value write, but the requested quality and source timestamp "
            "could not be verified; write outcome is uncertain"
        ) from exc
    actual_quality = _quality_from_status_code(written_data_value.StatusCode)
    actual_timestamp = written_data_value.SourceTimestamp
    expected_timestamp = timestamp.astimezone(timezone.utc)
    if actual_quality != quality or actual_timestamp != expected_timestamp:
        client._record_failed_request()
        raise OpcUaWriteMetadataUnsupportedError(
            "OPC UA server did not preserve the requested quality and source timestamp; "
            "current-value VQT writes are unsupported by this server"
        )


async def read_history_values(
    client: OpcUaClient,
    node_ids: list[str],
    start_time: datetime | None,
    end_time: datetime | None,
) -> dict[str, list[ua.DataValue]]:
    if not node_ids:
        return {}

    started = perf_counter()
    client._runtime_metrics.history_read_nodes += len(node_ids)
    limits = await client.get_operational_limits()
    batch_size = limits.max_nodes_per_history_read_data or len(node_ids)
    details = ua.ReadRawModifiedDetails(
        IsReadModified=False,
        StartTime=start_time if start_time is not None else ua.get_win_epoch(),
        EndTime=end_time if end_time is not None else ua.get_win_epoch(),
        NumValuesPerNode=0,
        ReturnBounds=False,
    )
    values_by_node_id: dict[str, list[ua.DataValue]] = {}
    history_batches = _chunked(node_ids, max(1, batch_size))
    semaphore = asyncio.Semaphore(client._browse_concurrency)

    async def read_history_batch(node_batch: list[str]) -> dict[str, list[ua.DataValue]]:
        async with semaphore:
            batch_values = await client._read_history_batch(details, node_batch)
            batch_values_by_node_id: dict[str, list[ua.DataValue]] = {node_id: [] for node_id in node_batch}
            active_node_ids = list(node_batch)
            active_results = batch_values

            while active_node_ids:
                next_node_ids: list[str] = []
                next_continuations: dict[str, bytes] = {}
                for node_id, result in zip(active_node_ids, active_results, strict=True):
                    try:
                        result.StatusCode.check()
                    except Exception as exc:
                        if client._is_history_missing_or_unsupported(exc):
                            logger.info(
                                "OPC UA history read returned no data or unsupported history endpoint=%s node_id=%s",
                                client._endpoint,
                                node_id,
                            )
                            batch_values_by_node_id[node_id] = []
                            continue
                        raise

                    history_data = result.HistoryData
                    data_values = getattr(history_data, "DataValues", None)
                    if not isinstance(data_values, list):
                        raise UaError(f"Incomplete OPC UA history response for {node_id}")
                    batch_values_by_node_id[node_id].extend(data_values)
                    continuation_point = result.ContinuationPoint
                    if continuation_point:
                        next_node_ids.append(node_id)
                        next_continuations[node_id] = continuation_point

                if not next_node_ids:
                    break
                active_results = await client._read_history_batch(
                    details,
                    next_node_ids,
                    [next_continuations[node_id] for node_id in next_node_ids],
                )
                active_node_ids = next_node_ids
            return batch_values_by_node_id

    try:
        batch_results = await _gather_cancelled_on_failure([read_history_batch(batch) for batch in history_batches])
        for batch_values in batch_results:
            values_by_node_id.update(batch_values)
    except Exception:
        client._record_failed_request()
        raise

    for _ in range(len(values_by_node_id)):
        client._record_history_read_success()

    logger.debug(
        "OPC UA history read ok endpoint=%s nodes=%d values=%d duration_s=%.3f",
        client._endpoint,
        len(node_ids),
        sum(len(item) for item in values_by_node_id.values()),
        perf_counter() - started,
    )
    return values_by_node_id


async def _read_history_batch(
    client: OpcUaClient,
    details: ua.ReadRawModifiedDetails,
    node_ids: list[str],
    continuation_points: list[bytes | None] | None = None,
) -> list[ua.HistoryReadResult]:
    points = continuation_points or [None] * len(node_ids)
    params = ua.HistoryReadParameters()
    params.HistoryReadDetails = details
    params.TimestampsToReturn = ua.TimestampsToReturn.Both
    params.ReleaseContinuationPoints = False
    params.NodesToRead = [
        ua.HistoryReadValueId(NodeId=client._client.get_node(node_id).nodeid, ContinuationPoint=point)
        for node_id, point in zip(node_ids, points, strict=True)
    ]

    client._runtime_metrics.history_read_calls += 1
    try:
        results = await client._client.uaclient.history_read(params)
    except Exception as exc:
        if client._should_retry_after_disconnect(exc):
            logger.warning(
                "OPC UA batched history read retry after reconnect endpoint=%s node_count=%d",
                client._endpoint,
                len(node_ids),
            )
            await client._wait_for_reconnect(exc)
            results = await client._retry_call(client._client.uaclient.history_read(params))
        elif client._is_too_many_operations_error(exc) and len(node_ids) > 1:
            split_at = max(1, len(node_ids) // 2)
            logger.warning(
                "OPC UA history read split retry endpoint=%s batch_size=%d split_at=%d",
                client._endpoint,
                len(node_ids),
                split_at,
            )
            left = await client._read_history_batch(details, node_ids[:split_at], points[:split_at])
            right = await client._read_history_batch(details, node_ids[split_at:], points[split_at:])
            return left + right
        else:
            client._record_failed_request()
            raise

    if len(results) != len(node_ids):
        raise UaError(f"HistoryRead returned {len(results)} results for {len(node_ids)} nodes")
    return results


def _is_history_missing_or_unsupported(exc: Exception) -> bool:
    lowered = str(exc).lower()
    return any(
        token in lowered
        for token in (
            "badhistoryoperationunsupported",
            "historyoperationunsupported",
            "historyread not supported",
            "history read not supported",
            "badnodata",
            "goodnodata",
            "no data exists for the requested time range",
        )
    )


async def call_method(client: OpcUaClient, object_node_id: str, method_node_id: str, args: list[Any]) -> Any:
    started = perf_counter()
    client._runtime_metrics.method_calls += 1
    object_node = client._client.get_node(object_node_id)
    try:
        # Do not replay a method call after a connection loss: it may already have run.
        result = await object_node.call_method(method_node_id, *args)
        client._record_method_success()
        logger.info(
            "OPC UA method ok object_node_id=%s method_node_id=%s arg_count=%d duration_s=%.3f",
            object_node_id,
            method_node_id,
            len(args),
            perf_counter() - started,
        )
        return result
    except Exception:
        client._record_failed_request()
        logger.exception(
            "OPC UA method failed object_node_id=%s method_node_id=%s arg_count=%d duration_s=%.3f",
            object_node_id,
            method_node_id,
            len(args),
            perf_counter() - started,
        )
        raise


async def read_server_status_data_value(client: OpcUaClient) -> ua.DataValue:
    values = await client.read_data_values(["i=2256"])
    if not values:
        client._record_failed_request()
        raise RuntimeError("OPC UA ServerStatus read returned no data value")
    return values[0]
