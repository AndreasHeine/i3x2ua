"""API-layer dependency functions for application services."""

from __future__ import annotations

from fastapi import Depends

from i3x_server.application.services.model_query import ModelQueryService
from i3x_server.dependencies import get_opcua_client, get_or_build_model
from i3x_server.domain.ports.opcua import OpcUaClientProtocol
from i3x_server.schemas.state import BuildResult


async def get_model_query_service(
    model: BuildResult = Depends(get_or_build_model),
    opcua_client: OpcUaClientProtocol = Depends(get_opcua_client),
) -> ModelQueryService:
    """Build ModelQueryService from API dependencies."""
    return ModelQueryService(opcua_client=opcua_client, model=model)
