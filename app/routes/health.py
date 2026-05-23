"""Liveness + readiness endpoints for Kubernetes probes."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import __version__
from ..db import get_session
from ..schemas import HealthResponse

router = APIRouter()


@router.get("/healthz", response_model=HealthResponse, tags=["meta"])
async def healthz() -> HealthResponse:
    """Liveness probe — always returns 200 if the app is up."""
    return HealthResponse(status="ok", db="ok", version=__version__)


@router.get("/readyz", response_model=HealthResponse, tags=["meta"])
async def readyz(session: AsyncSession = Depends(get_session)) -> HealthResponse:
    """Readiness probe — checks DB connectivity.

    Kubernetes will pull the pod out of the Service when this fails, which is
    exactly what we want during DB maintenance: events keep queueing on the
    devices and we don't drop them just because the API is briefly degraded.
    """
    try:
        await session.execute(text("SELECT 1"))
        db_status: str = "ok"
        overall: str = "ok"
    except Exception:
        db_status = "down"
        overall = "degraded"
    return HealthResponse(status=overall, db=db_status, version=__version__)
