"""
Model query orchestration service.

Handles retrieval and filtering of i3X model structure (namespaces, object types,
relationship types, and objects).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request

from i3x_server.domain.ports.opcua import OpcUaClientProtocol, OpcUaNamespaceInfo
from i3x_server.domain.utils import display_name_for_uri
from i3x_server.errors import i3x_http_error
from i3x_server.schemas.state import BuildResult
from i3x_server.version import get_server_version

logger = logging.getLogger(__name__)


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


def _build_server_info(server_version: str | None = None, server_name: str | None = None) -> ServerInfo:
    from i3x_server.domain.utils import server_name_from_openapi

    return ServerInfo(
        specVersion="1.0",
        serverVersion=server_version or get_server_version(),
        serverName=server_name or server_name_from_openapi(),
        capabilities=ServerCapabilities(
            query={"history": True},
            update={"current": False, "history": False},
            subscribe={"stream": True},
        ),
    )


class ModelQueryService:
    """Orchestrates model structure queries."""

    def __init__(
        self,
        opcua_client: OpcUaClientProtocol,
        model: BuildResult,
        request: Request | None = None,
    ):
        """Initialize service with dependencies.

        Args:
            opcua_client: OPC UA protocol client
            model: Pre-built model structure
            request: Optional FastAPI request for caching context
        """
        self.opcua_client = opcua_client
        self.model = model
        self.request = request
        self._namespace_cache: list[OpcUaNamespaceInfo] | None = None

    async def get_server_info(self) -> ServerInfo:
        """Retrieve server information.

        Returns:
            ServerInfo with capabilities
        """
        return _build_server_info()

    async def get_namespaces(self) -> list[Namespace]:
        """Retrieve all OPC UA namespaces.

        Returns:
            List of namespace objects with display names

        Raises:
            HTTPException: If namespace retrieval fails
        """
        try:
            if self._namespace_cache is None:
                self._namespace_cache = await self.opcua_client.get_namespace_infos()
            return [_to_namespace(item) for item in self._namespace_cache]
        except Exception as exc:
            raise i3x_http_error(
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
        if self._namespace_cache is None:
            self._namespace_cache = await self.opcua_client.get_namespace_infos()
        return self._namespace_cache or []

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
        # This will be populated with actual implementation
        # once we move the object type context building logic
        raise NotImplementedError("ObjectType retrieval moved to v1_monolithic; Phase 4b will extract")

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
        # Phase 4b: Extract relationship type logic
        raise NotImplementedError("RelationshipType retrieval in progress")

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
        # Phase 4b: Extract object retrieval logic
        raise NotImplementedError("Object retrieval in progress")
