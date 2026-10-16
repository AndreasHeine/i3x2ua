"""Documentation of middleware behavior not inferred from endpoint signatures."""

from typing import Any

from fastapi import FastAPI

from i3x_server.http_caching import MODEL_REVISION_HEADER

_CACHE_HEADERS = {
    "ETag": {
        "description": "Weak representation validator; may be omitted for degraded responses.",
        "schema": {"type": "string"},
    },
    MODEL_REVISION_HEADER: {
        "description": "Opaque revision of model metadata used to produce this response.",
        "schema": {"type": "string"},
    },
    "Cache-Control": {
        "description": "no-cache: stored responses must be revalidated before reuse.",
        "schema": {"type": "string"},
    },
}

CONDITIONAL_GET_OPENAPI: dict[str, Any] = {
    "parameters": [
        {
            "name": "If-None-Match",
            "in": "header",
            "required": False,
            "description": "ETag from an earlier response. A match returns 304 with no body.",
            "schema": {"type": "string"},
            "example": 'W/"opaque-representation-revision"',
        }
    ],
    "responses": {
        "200": {"headers": _CACHE_HEADERS},
        "304": {
            "description": "Not modified. Reuse the previously cached representation; no response body.",
            "headers": _CACHE_HEADERS,
        },
    },
}


class DocumentedFastAPI(FastAPI):
    def openapi(self) -> dict[str, Any]:
        schema = super().openapi()
        for path, operations in schema["paths"].items():
            if not path.startswith(("/v1/", "/ua/", "/mcp", "/model/")):
                continue
            for operation in operations.values():
                if not isinstance(operation, dict) or "responses" not in operation:
                    continue
                responses = operation["responses"]
                validation_response = responses.get("422", {})
                validation_schema = validation_response.get("content", {}).get("application/json", {}).get("schema", {})
                if validation_schema.get("$ref") == "#/components/schemas/HTTPValidationError":
                    del responses["422"]
        return schema
