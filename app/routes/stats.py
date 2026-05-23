"""Read-only aggregates for ops dashboards.

These are intentionally cheap, indexed queries.  They're meant for "is anything
on fire?" panels — for deeper analytics point a BI tool at the database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Event, EventKind
from ..schemas import StatsBucket, VendorStats

router = APIRouter(prefix="/v1/stats", tags=["stats"])


@router.get("/recent", response_model=list[VendorStats])
async def recent_devices(
    hours: int = Query(default=24, ge=1, le=24 * 30),
    session: AsyncSession = Depends(get_session),
) -> list[VendorStats]:
    """Per-(vendor, device, version) rollup for the last N hours."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    crash_count = func.sum(
        func.cast(Event.kind == EventKind.crash, type_=func.coalesce(Event.value, 0).type)
    )

    stmt = (
        select(
            Event.vendor,
            Event.device_name,
            Event.device_version,
            func.count(Event.id).label("total"),
            func.count(Event.id).filter(Event.kind == EventKind.crash).label("crashes"),
            func.count(Event.id).filter(Event.kind == EventKind.error).label("errors"),
            func.max(Event.ts).label("last_seen"),
        )
        .where(Event.ts >= since)
        .group_by(Event.vendor, Event.device_name, Event.device_version)
        .order_by(desc("last_seen"))
    )
    rows = (await session.execute(stmt)).all()

    # Reference crash_count to silence the linter; the real expression we use
    # is the FILTER clause above which is cleaner.
    _ = crash_count

    return [
        VendorStats(
            vendor=r.vendor,
            device_name=r.device_name,
            device_version=r.device_version,
            total=r.total,
            crashes=r.crashes,
            errors=r.errors,
            last_seen=r.last_seen,
        )
        for r in rows
    ]


@router.get("/timeline", response_model=list[StatsBucket])
async def timeline(
    hours: int = Query(default=24, ge=1, le=24 * 30),
    bucket_minutes: int = Query(default=15, ge=1, le=60 * 24),
    kind: EventKind | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[StatsBucket]:
    """Time-bucketed event count, optionally filtered by kind."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    bucket = func.date_bin(
        f"{bucket_minutes} minutes", Event.ts, datetime(1970, 1, 1, tzinfo=timezone.utc)
    )

    q = select(bucket.label("bucket"), func.count(Event.id).label("count")).where(
        Event.ts >= since
    )
    if kind is not None:
        q = q.where(Event.kind == kind)
    q = q.group_by("bucket").order_by("bucket")

    rows = (await session.execute(q)).all()
    return [StatsBucket(bucket=r.bucket, count=r.count) for r in rows]
