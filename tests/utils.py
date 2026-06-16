"""Shared test utilities for feature packages."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient


def fastapi_app(client: TestClient) -> FastAPI:
    """Extract FastAPI app from test client."""
    return cast(FastAPI, client.app)


def resolve_openapi_schema(schema: Any, components: Mapping[str, Any]) -> Any:
    """Resolve OpenAPI schema references."""
    if not isinstance(schema, Mapping):
        return schema

    if "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str):
            return dict(schema)
        prefix = "#/components/schemas/"
        if not ref.startswith(prefix):
            return {"$ref": ref}
        schema_name = ref.removeprefix(prefix)
        schemas = components.get("schemas", {})
        if isinstance(schemas, Mapping) and schema_name in schemas:
            return resolve_openapi_schema(schemas[schema_name], components)
        return {"$ref": ref}

    resolved: dict[str, Any] = dict(schema)
    for key in ("allOf", "anyOf", "oneOf"):
        value = resolved.get(key)
        if isinstance(value, list):
            resolved[key] = [resolve_openapi_schema(item, components) for item in value]

    properties = resolved.get("properties")
    if isinstance(properties, Mapping):
        resolved["properties"] = {
            str(name): resolve_openapi_schema(value, components) for name, value in properties.items()
        }

    items = resolved.get("items")
    if items is not None:
        resolved["items"] = resolve_openapi_schema(items, components)

    return resolved


def pick_concrete_schema(schema: Any) -> Any:
    """Pick the concrete schema from a union."""
    if not isinstance(schema, Mapping):
        return schema

    for key in ("anyOf", "oneOf"):
        variants = schema.get(key)
        if isinstance(variants, list) and variants:
            for variant in variants:
                if isinstance(variant, Mapping) and variant.get("type") == "null":
                    continue
                return pick_concrete_schema(variant)
            return pick_concrete_schema(variants[0])

    return schema


def sample_from_schema(schema: Any, *, property_name: str = "") -> Any:
    """Generate a sample value from a JSON schema."""
    concrete = pick_concrete_schema(schema)
    if not isinstance(concrete, Mapping):
        return "sample"

    enum_values = concrete.get("enum")
    if isinstance(enum_values, list) and enum_values:
        return enum_values[0]

    schema_type = concrete.get("type")
    if isinstance(schema_type, list):
        non_null_types = [item for item in schema_type if item != "null"]
        schema_type = non_null_types[0] if non_null_types else schema_type[0]

    if schema_type == "string":
        lowered = property_name.lower()
        if "time" in lowered:
            return "2026-01-01T00:00:00Z"
        if lowered.endswith("id") or lowered.endswith("ids"):
            return "property-abc"
        return "sample"

    if schema_type == "integer":
        minimum = concrete.get("minimum")
        if isinstance(minimum, int):
            return minimum
        return 1

    if schema_type == "number":
        minimum = concrete.get("minimum")
        if isinstance(minimum, (int, float)):
            return float(minimum)
        return 1.0

    if schema_type == "boolean":
        return False

    if schema_type == "array":
        item_schema = concrete.get("items", {"type": "string"})
        return [sample_from_schema(item_schema, property_name=property_name.removesuffix("s"))]

    properties = concrete.get("properties")
    if schema_type == "object" or isinstance(properties, Mapping):
        if not isinstance(properties, Mapping):
            return {}
        required = concrete.get("required", [])
        if not isinstance(required, list):
            required = []
        result: dict[str, Any] = {}
        for key in required:
            if not isinstance(key, str):
                continue
            result[key] = sample_from_schema(properties.get(key, {}), property_name=key)
        return result

    return {}


def build_required_mcp_arguments(input_schema: Mapping[str, Any]) -> dict[str, Any]:
    """Build required MCP tool arguments from input schema."""
    properties = input_schema.get("properties", {})
    required = input_schema.get("required", [])
    if not isinstance(properties, Mapping) or not isinstance(required, list):
        return {}

    args: dict[str, Any] = {}
    for name in required:
        if not isinstance(name, str):
            continue
        args[name] = sample_from_schema(properties.get(name, {}), property_name=name)
    return args


def with_runtime_argument_overrides(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    subscription_id: str | None,
) -> dict[str, Any]:
    """Apply runtime overrides to MCP tool arguments."""
    result = dict(arguments)

    if "elementId" in result:
        result["elementId"] = "property-abc"

    body = result.get("body")
    if isinstance(body, Mapping):
        body_dict = dict(body)
        if "elementIds" in body_dict:
            body_dict["elementIds"] = ["property-abc"]
        if tool_name == "queryHistoricalValues":
            body_dict.setdefault("startTime", "2026-01-01T00:00:00Z")
            body_dict.setdefault("endTime", "2026-01-02T00:00:00Z")
            body_dict.setdefault("maxDepth", 1)
        if tool_name == "updateObjectValue":
            body_dict = {"value": 123}
        if tool_name == "createSubscription":
            body_dict.setdefault("clientId", "mcp-runtime-smoke")
            body_dict.setdefault("displayName", "MCP Runtime Smoke")
        if subscription_id is not None:
            if "subscriptionId" in body_dict:
                body_dict["subscriptionId"] = subscription_id
            if "subscriptionIds" in body_dict:
                body_dict["subscriptionIds"] = [subscription_id]
        result["body"] = body_dict

    return result


def load_openapi_spec() -> dict[str, Any]:
    """Load the OpenAPI specification from file."""
    openapi_path = Path(__file__).resolve().parents[2] / "openapi.json"
    payload = json.loads(openapi_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        return payload
    return {}
