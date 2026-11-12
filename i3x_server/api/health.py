"""Static readiness probe for container healthchecks."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["Diagnostics"])


class HealthResponse(BaseModel):
    status: str = "ok"


@router.get("/health", response_model=HealthResponse, include_in_schema=False)
async def get_health() -> HealthResponse:
    """Report that the gateway process is serving.

    Deliberately static: probes must not touch the OPC UA connection or the model, so a
    reconnecting server never flips container health.
    """
    return HealthResponse()
