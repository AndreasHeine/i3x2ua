"""
Domain utilities for OPC UA node ID and namespace manipulation.

Shared utilities used across routers and services for consistent handling
of OPC UA identifiers and namespace resolution.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

from i3x_server.domain.ports.opcua import OpcUaNamespaceInfo


def normalize_namespace_uri(uri: str) -> str:
    """Normalize namespace URI for comparison.

    Args:
        uri: Namespace URI to normalize

    Returns:
        Normalized URI (lowercased, trailing slashes removed)
    """
    return uri.strip().rstrip("/").lower()


def canonical_namespace_uri(uri: str, namespace_infos: list[OpcUaNamespaceInfo]) -> str:
    """Resolve canonical namespace URI from list.

    Args:
        uri: Namespace URI to resolve
        namespace_infos: Available namespace definitions

    Returns:
        Canonical URI from the list, or original if not found
    """
    normalized = normalize_namespace_uri(uri)
    for item in namespace_infos:
        if normalize_namespace_uri(item.uri) == normalized:
            return item.uri
    return uri


def namespace_uri_for_node_id(node_id: str, namespace_infos: list[OpcUaNamespaceInfo]) -> str:
    """Extract namespace URI from indexed node ID.

    Args:
        node_id: OPC UA node ID (indexed format: ns=N;...)
        namespace_infos: Available namespace definitions

    Returns:
        Corresponding namespace URI, or empty string if not found
    """
    match = re.search(r"ns=(\d+)", node_id)
    namespace_index = int(match.group(1)) if match is not None else 0
    if 0 <= namespace_index < len(namespace_infos):
        return namespace_infos[namespace_index].uri
    return ""


def expanded_node_id(node_id: str, namespace_infos: list[OpcUaNamespaceInfo]) -> str:
    """Convert indexed node ID to expanded format (nsu=...;...).

    Args:
        node_id: OPC UA node ID (any format)
        namespace_infos: Available namespace definitions

    Returns:
        Node ID in expanded format
    """
    if node_id.startswith("nsu="):
        return node_id

    match = re.match(r"^(?:ns=(\d+);)?([isgb]=.+)$", node_id)
    if match is None:
        return node_id

    namespace_index = int(match.group(1)) if match.group(1) is not None else 0
    identifier = match.group(2)

    # Namespace index 0 is the OPC UA standard namespace.
    if namespace_index == 0:
        return f"nsu=http://opcfoundation.org/UA/;{identifier}"

    if not (0 <= namespace_index < len(namespace_infos)):
        return node_id

    namespace_uri = namespace_infos[namespace_index].uri
    if not namespace_uri:
        return node_id

    return f"nsu={namespace_uri};{identifier}"


def namespace_uri_from_expanded_node_id(node_id: str) -> str | None:
    """Extract namespace URI from expanded node ID.

    Args:
        node_id: OPC UA node ID (expanded format: nsu=...;...)

    Returns:
        Namespace URI, or None if format is invalid
    """
    match = re.match(r"^nsu=([^;]+);", node_id)
    if match is None:
        return None
    namespace_uri = match.group(1)
    return namespace_uri or None


def is_null_opcua_type_node_id(node_id: str) -> bool:
    """Check if node ID represents the null type (i=0).

    Args:
        node_id: OPC UA node ID (any format)

    Returns:
        True if node ID is the null type
    """
    normalized = node_id.strip()
    if re.match(r"^nsu=[^;]+;i=0$", normalized, flags=re.IGNORECASE):
        return True
    if re.match(r"^ns=\d+;i=0$", normalized, flags=re.IGNORECASE):
        return True
    return bool(re.match(r"^i=0$", normalized, flags=re.IGNORECASE))


def display_name_for_uri(uri: str) -> str:
    """Generate human-readable display name from namespace URI.

    Args:
        uri: Namespace URI

    Returns:
        Human-readable display name
    """
    parsed_path = uri.split("//", 1)[-1]
    tail = parsed_path.rsplit("/", 1)[-1] if "/" in parsed_path else parsed_path
    token = tail.replace("-", " ").replace("_", " ")
    if token:
        if any(ch.isdigit() for ch in token):
            return token.upper()
        return token.title()
    host = uri.split("//", 1)[-1].split(":", 1)[0].split(".")
    return host[0].title() if host and host[0] else uri


def namespace_infos_by_uri(namespace_infos: list[OpcUaNamespaceInfo]) -> dict[str, OpcUaNamespaceInfo]:
    """Index namespace infos by URI.

    Args:
        namespace_infos: List of namespace definitions

    Returns:
        Dictionary mapping URI to namespace info
    """
    return {item.uri: item for item in namespace_infos}


@lru_cache(maxsize=1)
def server_name_from_openapi(default_name: str = "The i3X API Gateway for OPC UA") -> str:
    """Read server name from OpenAPI document.

    Args:
        default_name: Fallback name if OpenAPI title not found

    Returns:
        Server name from OpenAPI or default
    """
    openapi_path = Path(__file__).resolve().parents[2] / "openapi.json"
    try:
        import json

        openapi_doc = json.loads(openapi_path.read_text(encoding="utf-8"))
        info = openapi_doc.get("info")
        if isinstance(info, Mapping):
            title = info.get("title")
            if isinstance(title, str) and title.strip():
                return title.strip()
    except Exception:
        pass
    return default_name
