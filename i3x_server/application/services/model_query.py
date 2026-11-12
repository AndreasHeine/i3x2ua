"""
Model query orchestration service.

Handles retrieval and filtering of i3X model structure (namespaces, object types,
relationship types, and objects).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from i3x_server.application.errors import ApplicationServiceError
from i3x_server.config.settings import get_settings
from i3x_server.domain.ports.opcua import OpcUaClientProtocol, OpcUaNamespaceInfo
from i3x_server.domain.utils import (
    canonical_namespace_uri,
    display_name_for_uri,
    namespace_uri_for_node_id,
    normalize_namespace_uri,
)
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult
from i3x_server.version import get_server_version

logger = logging.getLogger(__name__)

_I3X_NAMESPACE = "https://cesmii.org/i3x"


class NamespaceSnapshotCache:
    """Application-level holder for the namespace snapshot shared by all services.

    ``ModelQueryService`` instances are built per request, so a service-local cache never
    persists. The OPC UA client owns TTL invalidation and returns a new list object after a
    refresh or reconnect; the holder detects that identity change and rebuilds the converted
    view, so request-scoped services share one conversion instead of redoing it per request.
    """

    def __init__(self) -> None:
        self._infos: list[OpcUaNamespaceInfo] | None = None
        self._namespaces: list[Namespace] | None = None

    async def infos(self, opcua_client: OpcUaClientProtocol) -> list[OpcUaNamespaceInfo]:
        """Return the current namespace infos, refreshed when the client hands us a new list."""
        infos = await opcua_client.get_namespace_infos()
        if infos is not self._infos:
            self._infos = infos
            self._namespaces = None
        return infos

    async def namespaces(self, opcua_client: OpcUaClientProtocol) -> list[Namespace]:
        """Return the response view of the current namespace infos as a shallow copy."""
        infos = await self.infos(opcua_client)
        if self._namespaces is None:
            self._namespaces = [_to_namespace(item) for item in infos]
        return list(self._namespaces)


class Namespace:
    """Namespace response model."""

    def __init__(self, uri: str, displayName: str):
        self.uri = uri
        self.displayName = displayName

    def model_dump(self) -> dict[str, Any]:
        return {"uri": self.uri, "displayName": self.displayName}


class ServerCapabilities:
    """Server capability indicators."""

    def __init__(self, query: dict[str, Any], update: dict[str, Any], subscribe: dict[str, Any]):
        self.query = query
        self.update = update
        self.subscribe = subscribe

    def model_dump(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "update": self.update,
            "subscribe": self.subscribe,
        }


class ServerInfo:
    """Server info response model."""

    def __init__(
        self,
        specVersion: str,
        serverVersion: str | None = None,
        serverName: str | None = None,
        capabilities: ServerCapabilities | None = None,
    ):
        self.specVersion = specVersion
        self.serverVersion = serverVersion
        self.serverName = serverName
        self.capabilities = capabilities

    def model_dump(self) -> dict[str, Any]:
        return {
            "specVersion": self.specVersion,
            "serverVersion": self.serverVersion,
            "serverName": self.serverName,
            "capabilities": self.capabilities.model_dump() if self.capabilities else None,
        }


def _to_namespace(item: OpcUaNamespaceInfo) -> Namespace:
    display_name = item.display_name or display_name_for_uri(item.uri)
    return Namespace(uri=item.uri, displayName=display_name)


def _build_server_info(
    server_version: str | None = None,
    server_name: str | None = None,
    history_update_supported: bool | None = None,
) -> ServerInfo:
    from i3x_server.domain.utils import server_name_from_openapi

    writes_enabled = bool(get_settings().enable_writes)

    return ServerInfo(
        specVersion="1.0",
        serverVersion=server_version or get_server_version(),
        serverName=server_name or server_name_from_openapi(),
        capabilities=ServerCapabilities(
            # Honest declaration: the history endpoint is always implemented and never errors on a
            # range without stored samples (GoodNoData), unlike update.history, which depends on the
            # upstream server actually exposing history writes (spec :599-601, :1294).
            query={"history": True},
            update={
                "current": writes_enabled,
                "history": writes_enabled and history_update_supported is not False,
            },
            subscribe={"stream": True},
        ),
    )


def _element_id_from_node_id(prefix: str, node_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", node_id).strip("-")
    return f"{prefix}-{cleaned.lower()}" if cleaned else f"{prefix}-unknown"


def _relationship_type_items(model: BuildResult | None = None) -> list[dict[str, str]]:
    items = [
        {
            "elementId": "HasParent",
            "displayName": "HasParent",
            "namespaceUri": _I3X_NAMESPACE,
            "relationshipId": "HasParent",
            "reverseOf": "HasChildren",
        },
        {
            "elementId": "HasChildren",
            "displayName": "HasChildren",
            "namespaceUri": _I3X_NAMESPACE,
            "relationshipId": "HasChildren",
            "reverseOf": "HasParent",
        },
        {
            "elementId": "HasComponent",
            "displayName": "HasComponent",
            "namespaceUri": _I3X_NAMESPACE,
            "relationshipId": "HasComponent",
            "reverseOf": "ComponentOf",
        },
        {
            "elementId": "ComponentOf",
            "displayName": "ComponentOf",
            "namespaceUri": _I3X_NAMESPACE,
            "relationshipId": "ComponentOf",
            "reverseOf": "HasComponent",
        },
    ]
    if model is None:
        return items

    graph_names = getattr(model, "graph_relationship_names", None) or set()
    inverse_by_id = getattr(model, "graph_relationship_inverse_by_id", None) or {}
    existing = {item["elementId"] for item in items}
    graph_names = {name for name in graph_names if name not in existing}
    emitted = set(existing)
    for name in sorted(graph_names):
        namespace_uri = model.graph_relationship_namespace_by_id[name]
        display_name = model.graph_relationship_display_name_by_id.get(name, name)
        # Prefer the inverse the address space actually models: the natural relationship name of the
        # opposite direction (e.g. `MonitoredBy`) or the reference type's `InverseName` attribute
        # (e.g. `NotifierOf`); `inverseOf_<name>` is only invented when the source defines neither
        # (spec :397 requires one).
        reverse_name = inverse_by_id.get(name) or f"inverseOf_{name}"
        if reverse_name.startswith("inverseOf_"):
            reverse_display = f"inverseOf_{display_name}"
        else:
            reverse_display = model.graph_relationship_display_name_by_id.get(
                reverse_name, reverse_name.rsplit(":", 1)[-1]
            )
        items.append(
            {
                "elementId": name,
                "displayName": display_name,
                "namespaceUri": namespace_uri,
                "relationshipId": name,
                "reverseOf": reverse_name,
            }
        )
        emitted.add(name)
        if reverse_name in graph_names or reverse_name in emitted:
            # The reverse is a registered relationship itself, or was already emitted as the
            # reverse of another forward; a second record would duplicate the elementId and leave
            # a dangling `reverseOf` chain.
            continue
        items.append(
            {
                "elementId": reverse_name,
                "displayName": reverse_display,
                "namespaceUri": namespace_uri,
                "relationshipId": reverse_name,
                "reverseOf": name,
            }
        )
        emitted.add(reverse_name)
    return items


def _type_namespace_uri(model: BuildResult, node: ModelNode) -> str | None:
    """Namespace of the node's *type* (spec :284: instances themselves belong to no namespace).

    ``node.type`` / ``node.source_type_id`` may be an expanded ``nsu=`` id or an indexed ``ns=N`` id;
    indexed ids are resolved through the namespace table captured during the model build.
    """
    raw = node.type or node.source_type_id
    if not raw:
        return None
    if raw.startswith("nsu="):
        return raw[len("nsu=") :].split(";", 1)[0] or None
    index_match = re.match(r"^ns=(\d+);", raw, flags=re.IGNORECASE)
    if index_match is None:
        return None
    return model.namespace_uri_by_index.get(int(index_match.group(1)))


def _to_object_instance(model: BuildResult, node: ModelNode, include_metadata: bool) -> dict[str, object]:
    metadata: dict[str, object] | None = None
    if include_metadata:
        metadata = {
            # Provenance fields are required whenever metadata is returned (spec :284, EXP-17).
            "typeNamespaceUri": _type_namespace_uri(model, node),
            "sourceTypeId": node.source_type_id or node.source_node_id,
            "description": str(node.metadata.get("description")) if "description" in node.metadata else None,
            "relationships": node.relationships,
            "extendedAttributes": node.metadata,
        }

    return {
        "elementId": node.id,
        "displayName": node.name,
        "typeElementId": node.type or node.source_type_id or node.kind,
        "parentId": node.parent_id,
        "isComposition": bool(node.is_composition),
        "isExtended": bool(node.metadata),
        "metadata": metadata,
    }


class ModelQueryService:
    """Orchestrates model structure queries."""

    def __init__(
        self,
        opcua_client: OpcUaClientProtocol,
        model: BuildResult,
        namespace_cache: NamespaceSnapshotCache | None = None,
    ):
        """Initialize service with dependencies.

        Args:
            opcua_client: OPC UA protocol client
            model: Pre-built model structure
            namespace_cache: Optional shared namespace snapshot; when omitted the service
                caches namespace infos locally for its own lifetime.
        """
        self.opcua_client = opcua_client
        self.model = model
        self._namespace_cache = namespace_cache
        self._local_namespace_infos: list[OpcUaNamespaceInfo] | None = None

    async def get_server_info(self) -> ServerInfo:
        """Retrieve server information.

        Returns:
            ServerInfo with capabilities
        """
        history_update_supported = getattr(self.opcua_client, "history_update_supported", None)
        return _build_server_info(
            history_update_supported=history_update_supported() if callable(history_update_supported) else None,
        )

    async def get_namespaces(self) -> list[Namespace]:
        """Retrieve all OPC UA namespaces.

        Returns:
            List of namespace objects with display names

        Raises:
            HTTPException: If namespace retrieval fails
        """
        try:
            if self._namespace_cache is not None:
                return await self._namespace_cache.namespaces(self.opcua_client)
            if self._local_namespace_infos is None:
                self._local_namespace_infos = await self.opcua_client.get_namespace_infos()
            return [_to_namespace(item) for item in self._local_namespace_infos]
        except Exception as exc:
            raise ApplicationServiceError(
                502,
                "OpcUaNamespaceError",
                "Failed to read OPC UA namespaces",
                {"cause": str(exc)},
            ) from exc

    async def get_namespace_infos(self) -> list[OpcUaNamespaceInfo]:
        """Retrieve raw namespace infos (internal use).

        Returns:
            List of namespace info objects
        """
        if self._namespace_cache is not None:
            return await self._namespace_cache.infos(self.opcua_client) or []
        if self._local_namespace_infos is None:
            self._local_namespace_infos = await self.opcua_client.get_namespace_infos()
        return self._local_namespace_infos or []

    async def get_object_types(
        self,
        namespace_uri: str | None = None,
    ) -> list[Any]:
        """Retrieve object types with optional namespace filtering.

        Args:
            namespace_uri: Optional namespace URI filter

        Returns:
            List of object type responses

        Raises:
            HTTPException: If object type retrieval fails
        """
        try:
            object_types = await self.opcua_client.get_object_types()
            namespace_infos = await self.get_namespace_infos()
        except Exception as exc:
            raise ApplicationServiceError(
                502,
                "OpcUaObjectTypeError",
                "Failed to read OPC UA object types",
                {"cause": str(exc)},
            ) from exc

        effective_namespace_uri: str | None = None
        if namespace_uri is not None:
            effective_namespace_uri = canonical_namespace_uri(namespace_uri, namespace_infos)

        results: list[dict[str, object]] = []
        for item in object_types:
            item_namespace_uri = namespace_uri_for_node_id(item.node_id, namespace_infos)
            if effective_namespace_uri is not None and item_namespace_uri != effective_namespace_uri:
                continue
            results.append(
                {
                    "elementId": _element_id_from_node_id("objecttype", item.node_id),
                    "displayName": item.display_name,
                    "namespaceUri": item_namespace_uri,
                    "sourceTypeId": item.node_id,
                    "version": None,
                    "schema": {},
                    "related": None,
                }
            )
        return results

    async def get_relationship_types(
        self,
        namespace_uri: str | None = None,
    ) -> list[Any]:
        """Retrieve relationship types with optional namespace filtering.

        Args:
            namespace_uri: Optional namespace URI filter

        Returns:
            List of relationship type responses
        """
        items = _relationship_type_items(self.model)
        if namespace_uri is None:
            return items
        # Namespace comparison follows mapping profile rule 6: canonicalized URIs,
        # so `https://cesmii.org/i3x/` matches the stored `https://cesmii.org/i3x`.
        normalized_filter = normalize_namespace_uri(namespace_uri)
        return [item for item in items if normalize_namespace_uri(str(item["namespaceUri"])) == normalized_filter]

    async def get_objects(
        self,
        element_ids: list[str] | None = None,
        include_metadata: bool = False,
    ) -> list[Any]:
        """Retrieve objects with optional element ID filtering.

        Args:
            element_ids: Optional list of element IDs to retrieve
            include_metadata: Whether to include full metadata

        Returns:
            List of object responses
        """
        nodes = list(self.model.nodes_by_id.values())
        if element_ids is not None:
            wanted = set(element_ids)
            nodes = [node for node in nodes if node.id in wanted]
        return [_to_object_instance(self.model, node, include_metadata) for node in nodes]
