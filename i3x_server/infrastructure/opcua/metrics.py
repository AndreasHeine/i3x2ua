"""Request/runtime metric recording for the OPC UA adapter.

The counters live on the client's typed ``runtime_metrics``/``request_metrics``
attributes; this module owns the success/failure classification and the recording
entry points the seam modules call through the client.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from i3x_server.domain.ports.opcua import OpcUaRequestMetrics, OpcUaRuntimeMetrics

if TYPE_CHECKING:
    from asyncua import ua

    from i3x_server.infrastructure.opcua.client import OpcUaClient

# Status-code names counted as a successful request. Uncertain counts by default:
# a value delivered with uncertain quality is still a delivered value (PR-09 semantics).
_GOODISH_QUALITY_LABELS = {"good", "uncertain"}


def reset_runtime_metrics(client: OpcUaClient) -> None:
    client.runtime_metrics = OpcUaRuntimeMetrics()
    client.request_metrics = OpcUaRequestMetrics()


def snapshot_runtime_metrics(client: OpcUaClient) -> OpcUaRuntimeMetrics:
    return OpcUaRuntimeMetrics(**asdict(client.runtime_metrics))


def snapshot_request_metrics(client: OpcUaClient) -> OpcUaRequestMetrics:
    return OpcUaRequestMetrics(**asdict(client.request_metrics))


def is_goodish_status(status_code: Any) -> bool:
    if status_code is None:
        return True
    is_uncertain = getattr(status_code, "is_uncertain", None)
    if callable(is_uncertain):
        try:
            if bool(is_uncertain()):
                return "uncertain" in _GOODISH_QUALITY_LABELS
        except Exception:
            pass
    is_good = getattr(status_code, "is_good", None)
    if callable(is_good):
        try:
            if bool(is_good()):
                return "good" in _GOODISH_QUALITY_LABELS
        except Exception:
            pass
    name = str(getattr(status_code, "name", status_code)).strip().lower()
    if not name:
        return False
    if "uncertain" in name:
        return "uncertain" in _GOODISH_QUALITY_LABELS
    if "good" in name:
        return "good" in _GOODISH_QUALITY_LABELS
    return name in _GOODISH_QUALITY_LABELS


def record_read_success(client: OpcUaClient) -> None:
    client.request_metrics.read_count += 1


def record_history_read_success(client: OpcUaClient) -> None:
    client.request_metrics.history_read_count += 1


def record_method_success(client: OpcUaClient) -> None:
    client.request_metrics.method_call_count += 1


def record_failed_request(client: OpcUaClient) -> None:
    client.request_metrics.failed_request_count += 1


def record_browse_results(client: OpcUaClient, results: list[ua.BrowseResult]) -> None:
    if not results:
        client.record_failed_request()
        return
    if all(is_goodish_status(getattr(result, "StatusCode", None)) for result in results):
        client.request_metrics.browse_count += 1
        return
    client.record_failed_request()


def record_read_data_values(client: OpcUaClient, values: list[ua.DataValue]) -> None:
    if not values:
        client.record_failed_request()
        return
    if all(is_goodish_status(value.StatusCode) for value in values):
        client.record_read_success()
        return
    client.record_failed_request()
