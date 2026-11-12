"""Legacy compatibility surface for the ``api/v1`` package.

The real implementations live in ``objecttype_helpers``, ``object_helpers``,
``subscription_routes`` and ``object_value_routes``. This module only re-exports
the names older callers still import and keeps the small helpers those callers
use directly.

The duplicated ``_ObjectTypeContext`` build/cache machinery that used to live
here was removed: it wrote the same ``app.state.object_type_context_cache`` as
``objecttype_helpers`` but with a different context class, which was a
cache-poisoning hazard. ``objecttype_helpers`` is the single production path.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from i3x_server.api.v1.common_helpers import (
    _raise_invalid_argument,
    _to_json_safe_value,
)
from i3x_server.api.v1.contracts import (
    GetObjectHistoryRequest,
    StreamRequest,
)
from i3x_server.api.v1.objecttype_helpers import (
    _scalar_schema_for_standard_ua_datatype_node_id,
)
from i3x_server.application.ports.opcua import OpcUaClientProtocol
from i3x_server.config.settings import Settings, get_settings
from i3x_server.domain.utils import (
    canonical_namespace_uri as _canonical_namespace_uri,
)
from i3x_server.domain.utils import (
    expanded_node_id as _expanded_node_id,
)
from i3x_server.domain.utils import (
    namespace_uri_for_node_id as _namespace_uri_for_node_id,
)
from i3x_server.domain.utils import (
    namespace_uri_from_expanded_node_id as _namespace_uri_from_expanded_node_id,
)
from i3x_server.errors import i3x_http_error

logger = logging.getLogger(__name__)

__all__ = [
    "GetObjectHistoryRequest",
    "StreamRequest",
    "_canonical_namespace_uri",
    "_expanded_node_id",
    "_namespace_uri_for_node_id",
    "_namespace_uri_from_expanded_node_id",
    "_scalar_schema_for_standard_ua_datatype_node_id",
    "stream_subscription_v1",
    "_raise_invalid_argument",
    "_to_json_safe_value",
    "_namespace_index_from_node_id",
    "_stream_debug_enabled",
    "_writes_enabled",
    "_not_implemented",
    "logger",
]


def _runtime_settings() -> Settings:
    return get_settings()


def _stream_debug_enabled() -> bool:
    return _runtime_settings().debug_subscription_stream


def _writes_enabled() -> bool:
    return _runtime_settings().enable_writes


def _not_implemented(feature: str) -> None:
    raise i3x_http_error(
        501,
        "NotImplemented",
        f"{feature} is not implemented in this server",
        {"feature": feature},
    )


def _namespace_index_from_node_id(node_id: str) -> int | None:
    match = re.match(r"^ns=(\d+);", node_id)
    if match is None:
        # OPC UA defaults to namespace index 0 when the ns prefix is omitted.
        if re.match(r"^[isgb]=", node_id):
            return 0
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


async def stream_subscription_v1(
    body: StreamRequest,
    opcua_client: OpcUaClientProtocol,
    subscription_app_service: Any,
) -> Any:
    """Compatibility shim for tests importing stream handler from monolithic module."""
    from i3x_server.api.v1.subscription_routes import stream_subscription_v1 as _stream_subscription_v1

    return await _stream_subscription_v1(
        body=body,
        opcua_client=opcua_client,
        subscription_app_service=subscription_app_service,
    )
