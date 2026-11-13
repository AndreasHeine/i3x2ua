"""OpenAPI document metadata for the gateway app.

Everything the published document says about the gateway - description, tags, contact, license,
external docs, telemetry, and the shared error-response map - lives here so ``app_factory`` only
assembles the app. The description is contract text (error semantics, production expectations),
so it belongs with the document it describes rather than the composition root.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi.routing import APIRoute
from fastapi.telemetry import TelemetryConfig

from i3x_server.api.v1.contracts import HttpErrorResponse
from i3x_server.config.settings import settings

# Contract text shown in `/docs` and the OpenAPI document: error semantics, bulk-result
# semantics, and the production expectations the README documents alongside it.
APP_DESCRIPTION = (
    "Turn any OPC UA server into an i3X-compliant REST and MCP Enabled API with OpenAPI docs, "
    "JSON, and live SSE streams. No OPC UA expertise required.\n\n"
    "**Workflow:** discover namespaces, types, and objects; use returned element IDs to read or write values. "
    "For changes, create a subscription, register objects, then sync or stream updates, and delete it when done. "
    "Example IDs are placeholders and must be replaced with IDs returned by this server.\n\n"
    "**Errors:** request validation returns HTTP 400, not 422. HTTP errors use the i3X JSON envelope with "
    "`success=false`, `error`, and `responseDetail`. Bulk operations can return HTTP 200 with failed items; "
    "inspect every item's `success` and `responseDetail` rather than relying only on the HTTP status.\n\n"
    "**Production deployment:** terminate HTTPS and enforce authentication/authorization at the configured "
    "reverse proxy. `/v1/info` must remain available without authentication. No HTTP authentication scheme "
    "is enforced by this application itself, so no deployment-specific security scheme is advertised here. "
    "A subscription `clientId` scopes subscriptions; it does not authenticate a caller.\n\n"
    "**Capabilities:** writes require `I3X_ENABLE_WRITES` and upstream permissions. Historical writes also "
    "require upstream HistoryUpdate support. Diagnostics and MCP are gateway extensions, not i3X core routes."
)

APP_CONTACT = {"name": "i3x2ua project", "url": "https://github.com/AndreasHeine/i3x2ua"}
APP_LICENSE_INFO = {
    "name": "AGPL-3.0 or commercial license",
    "url": "https://github.com/AndreasHeine/i3x2ua/blob/master/LICENSE",
}
OPENAPI_EXTERNAL_DOCS = {
    "description": "Deployment, usage, and OPC UA mapping documentation",
    "url": "https://github.com/AndreasHeine/i3x2ua#readme",
}

# Tag order mirrors the i3X route groups so `/docs` groups core routes before gateway extensions.
OPENAPI_TAGS: list[dict[str, str]] = [
    {"name": "Info", "description": "Retrieve general information about the server and its capabilities."},
    {"name": "Exploratory", "description": "Browse and discover the address space."},
    {"name": "Query", "description": "Read current or historical values of Objects."},
    {"name": "Update", "description": "Write current or historical data to Objects."},
    {"name": "Subscribe", "description": "Subscribe to data changes for Objects."},
    {"name": "Diagnostics", "description": "Inspect model metrics and OPC UA connectivity."},
    {
        "name": "MCP Protocol",
        "description": ("The JSON-RPC endpoint for MCP requests. MCP clients send requests to POST /mcp."),
    },
    {
        "name": "MCP Helpers",
        "description": (
            "Local REST-style convenience endpoints for inspecting and invoking MCP tools, prompts, "
            "resources, and model roots. These are not MCP protocol routes; MCP clients should use POST /mcp."
        ),
    },
]

# Applied to every router so the OpenAPI document advertises the i3X error envelope for the
# statuses the exception handlers actually produce.
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {
        "model": HttpErrorResponse,
        "description": "Invalid request, including body or parameter validation errors.",
    },
    500: {"model": HttpErrorResponse, "description": "Internal server error."},
}


def telemetry_config() -> TelemetryConfig:
    """OpenTelemetry switches handed to ``DocumentedFastAPI``.

    Read from the import-time settings snapshot, like every other document field: the document
    is built once at startup, so re-parsing the environment per app would only change what the
    schema advertises mid-process.
    """
    return {
        "tracing": settings.otel_enabled,
        "metrics": settings.otel_enabled,
        "logs": False,
        "operation_spans": settings.otel_enabled,
        "auto_configure": settings.otel_enabled,
    }


def _to_lower_camel_case(value: str) -> str:
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", value) if part]
    if not parts:
        return "operation"
    return parts[0].lower() + "".join(part[:1].upper() + part[1:] for part in parts[1:])


def _readable_operation_id(route: APIRoute) -> str:
    """Human-readable operationId derived from the route name, with a path-based fallback.

    FastAPI's default ids are function-name-plus-path hashes, which are unreadable in generated
    clients; MCP tool definitions consume these ids as tool names, so they must stay stable and
    descriptive.
    """
    route_name = str(getattr(route, "name", "") or "")
    candidate = _to_lower_camel_case(route_name)
    if candidate != "operation":
        return candidate

    methods = sorted(method for method in (route.methods or set()) if method not in {"HEAD", "OPTIONS"})
    method = methods[0].lower() if methods else "call"
    path_hint = route.path_format.strip("/").replace("/", "_").replace("{", "").replace("}", "")
    return _to_lower_camel_case(f"{method}_{path_hint}")
