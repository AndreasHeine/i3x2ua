"""Adapter seam regression guard (PR-08).

Sibling seam modules (``browse.py``, ``metadata.py``, ``node_reading.py``,
``object_types.py``, ``values.py``, ``recovery.py``) must reach the owning client
through public seams only; the asyncua bypasses go through the adapter-owned
escape hatches. A re-introduced ``client._private`` access would couple the
seams to client internals and silently break monkeypatch-based tests.
"""

from __future__ import annotations

import re
from pathlib import Path

from i3x_server.infrastructure.opcua.client import OpcUaClient

_PRIVATE_CLIENT_ACCESS = re.compile(r"\bclient\._\w+")

# Members the seams rely on: typed state, recording entry points, retry plumbing and
# the explicit asyncua escape hatches.
PUBLIC_SEAMS = (
    "endpoint",
    "raw_client",
    "browse_roots",
    "browse_concurrency",
    "runtime_metrics",
    "request_metrics",
    "limits_cache",
    "subscription_caps_cache",
    "namespace_infos_cache",
    "object_type_cache",
    "history_update_support",
    "reconnect_request_timeout_seconds",
    "browse_raw",
    "browse_next_raw",
    "raw_node",
    "resolve_browse_roots",
    "browse_children_descriptions",
    "browse_references_descriptions",
    "browse_with_retry",
    "browse_next_with_retry",
    "read_slot",
    "retry_call",
    "should_retry_after_disconnect",
    "wait_for_reconnect",
    "record_read_success",
    "record_read_data_values",
    "record_browse_results",
    "record_failed_request",
    "record_history_read_success",
    "record_method_success",
    "is_metadata_cache_entry_fresh",
    "invalidate_metadata_entry",
    "extract_attribute_value",
    "coerce_node_class",
)

LEGACY_PRIVATES = (
    "_endpoint",
    "_client",
    "_runtime_metrics",
    "_request_metrics",
    "_history_update_supported",
    "_browse_roots",
    "_object_types_cache",
    "_read_slot",
    "_retry_call",
    "_record_failed_request",
    "_should_retry_after_disconnect",
    "_wait_for_reconnect",
)


def test_sibling_adapter_modules_do_not_access_client_privates() -> None:
    module_dir = Path(__file__).resolve().parents[1] / "i3x_server" / "infrastructure" / "opcua"
    offenders: list[str] = []
    for path in sorted(module_dir.glob("*.py")):
        if path.name == "client.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _PRIVATE_CLIENT_ACCESS.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == [], "sibling adapter modules must use public client seams:\n" + "\n".join(offenders)


def test_client_exposes_typed_seams() -> None:
    client = OpcUaClient("opc.tcp://127.0.0.1:4840")
    for seam in PUBLIC_SEAMS:
        assert hasattr(client, seam), f"missing public seam {seam}"


def test_legacy_private_members_are_gone() -> None:
    client = OpcUaClient("opc.tcp://127.0.0.1:4840")
    for legacy in LEGACY_PRIVATES:
        assert not hasattr(client, legacy), f"legacy private member {legacy} still exists"
