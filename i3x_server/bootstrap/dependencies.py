"""Bootstrap dependency re-exports for route wiring.

The app-state lookups and the shared model-build dependency live in
``i3x_server.application.dependencies`` (the model cache machinery in
``application.services.model_cache``); bootstrap routes consume them through
this module so the previously duplicated implementation is retired.
"""

from __future__ import annotations

from i3x_server.application.dependencies import (
    get_model_builder,
    get_opcua_client,
    get_or_build_model,
    get_subscription_service,
)

__all__ = ["get_model_builder", "get_opcua_client", "get_or_build_model", "get_subscription_service"]
