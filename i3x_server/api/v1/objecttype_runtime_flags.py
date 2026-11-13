"""Runtime feature flags for object type serving.

The live BrowseName lookup values are snapshotted at import time: they gate per-request
lookup budgets, so one process-wide value is intentional. Tests patch these module
attributes (or the accessor functions here) to change the behavior. Callers reach the
accessors through this module so a patch applies to every call site at once.
"""

from __future__ import annotations

from i3x_server.config.settings import get_settings

_ENABLE_LIVE_TYPE_NAME_LOOKUP: bool = get_settings().enable_type_browsename_lookup
_LIVE_TYPE_NAME_LOOKUP_TIMEOUT_S: float = get_settings().type_browsename_lookup_timeout_s
_LIVE_TYPE_NAME_LOOKUP_MAX_PER_REQUEST: int = get_settings().type_browsename_lookup_max


def _live_type_name_lookup_enabled() -> bool:
    return _ENABLE_LIVE_TYPE_NAME_LOOKUP


def _live_type_name_lookup_timeout_seconds() -> float:
    return _LIVE_TYPE_NAME_LOOKUP_TIMEOUT_S


def _live_type_name_lookup_max_per_request() -> int:
    return _LIVE_TYPE_NAME_LOOKUP_MAX_PER_REQUEST


def _include_mcp_opcua_metadata() -> bool:
    return get_settings().mcp_include_opcua_metadata
