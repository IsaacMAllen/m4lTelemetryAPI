"""POST /v1/events — the receiver for bz.telemetry.

Behaviour
---------
- Accepts `application/x-ndjson` (preferred) and `application/json` (a single
  object or an array of objects, useful for curl tests).
- Authenticates with a bearer token if TELEMETRY_INGEST_TOKENS is set.
- Validates each line independently against IngestEvent and skips bad rows
  with a soft error in the response, so a single rotten event never wedges
  a device's queue.
- Inserts everything in one INSERT ... VALUES batch — much faster under load
  than per-row commits.
- Returns 200 with `{accepted, rejected, errors}` for the happy path.

Error semantics (relevant to the client retry logic in telemetry_core.cpp):
  - 401/403 → device pauses uploading until the user fixes the token.
  - 413     → device gives up on the current batch (we refuse to accept it).
  - 422     → entire body is unparseable (rare; bz.telemetry never emits
              malformed lines, but a misbehaving sibling client might).
  - 5xx     → device retries on the next flush interval.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import ValidationError
from sqlalchemy import desc, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_ingest_token
from ..config import Settings, get_settings
from ..db import get_session
from ..models import Event, EventKind, EventLevel
from ..ndjson import parse_ndjson
from ..schemas import EventListResponse, EventOut, IngestEvent, IngestResponse

router = APIRouter(prefix="/v1", tags=["ingest"])

log = logging.getLogger(__name__)


def _to_orm_row(ev: IngestEvent) -> dict[str, Any]:
    """Convert a validated IngestEvent into a dict suitable for bulk INSERT."""
    return {
        "vendor": ev.vendor,
        "device_name": ev.device_name,
        "device_version": ev.device_version,
        "device_id": ev.device_id,
        "session_id": ev.session_id,
        "user_id": ev.user_id,
        "platform": ev.platform,
        "max_version": ev.max_version,
        "kind": EventKind(ev.type),
        "level": EventLevel(ev.level),
        "name": ev.name,
        "message": ev.message,
        "ts": ev.ts,
        "ts_ms": ev.ts_ms,
        "value": ev.value,
        "unit": ev.unit,
        "props": ev.props,
    }


@router.post(
    "/events",
    response_model=IngestResponse,
    dependencies=[Depends(require_ingest_token)],
    summary="Ingest a batch of telemetry events",
)
async def ingest_events(
    request: Request,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> IngestResponse:
    raw = await request.body()

    if len(raw) > settings.max_body_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"body {len(raw)} bytes exceeds limit "
                f"{settings.max_body_bytes}"
            ),
        )

    content_type = request.headers.get("content-type", "").lower()

    # ---- Parse ---------------------------------------------------------------
    raw_objects: list[dict[str, Any]]
    parse_errors: list[str]
    if "ndjson" in content_type or "\n" in raw.decode("utf-8", errors="replace"):
        raw_objects, parse_errors = parse_ndjson(raw)
    else:
        # application/json: tolerate a single object or an array.
        try:
            import orjson
            payload = orjson.loads(raw or b"null")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"could not parse JSON body: {exc}",
            ) from exc
        if payload is None:
            raw_objects, parse_errors = [], []
        elif isinstance(payload, dict):
            raw_objects, parse_errors = [payload], []
        elif isinstance(payload, list):
            raw_objects, parse_errors = [], []
            for i, item in enumerate(payload, start=1):
                if isinstance(item, dict):
                    raw_objects.append(item)
                else:
                    parse_errors.append(f"item {i}: must be an object")
        else:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="JSON body must be an object or array of objects",
            )

    if len(raw_objects) > settings.max_events_per_request:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"{len(raw_objects)} events exceed per-request cap "
                f"{settings.max_events_per_request}"
            ),
        )

    # ---- Validate -----------------------------------------------------------
    rows: list[dict[str, Any]] = []
    errors: list[str] = list(parse_errors)
    for idx, obj in enumerate(raw_objects, start=1):
        try:
            ev = IngestEvent.model_validate(obj)
        except ValidationError as exc:
            errors.append(f"event {idx}: {exc.errors(include_url=False)}")
            continue
        rows.append(_to_orm_row(ev))

    # ---- Persist -------------------------------------------------------------
    if rows:
        # Use the dialect-specific INSERT so we can later add ON CONFLICT
        # behaviour (e.g. dedupe on (device_id, ts_ms, name) if a device
        # double-flushes a queue).  Today it's a plain bulk insert.
        stmt = pg_insert(Event).values(rows)
        await session.execute(stmt)

    accepted = len(rows)
    rejected = len(raw_objects) + len(parse_errors) - accepted
    if errors:
        log.warning(
            "ingest: accepted=%d rejected=%d first_error=%s",
            accepted,
            rejected,
            errors[0],
        )

    return IngestResponse(accepted=accepted, rejected=rejected, errors=errors[:25])


# ---------------------------------------------------------------------------
# Read endpoints (consumed by the bytr web UI + ad-hoc curl)
# ---------------------------------------------------------------------------
def _row_to_out(row: Event) -> EventOut:
    """Project an ORM row to the wire-shape used by GET /v1/events.

    We stringify enums + UUIDs here so consumers don't have to.
    """
    return EventOut(
        id=str(row.id),
        received_at=row.received_at,
        vendor=row.vendor,
        device_name=row.device_name,
        device_version=row.device_version,
        device_id=row.device_id,
        session_id=row.session_id,
        user_id=row.user_id,
        platform=row.platform,
        max_version=row.max_version,
        kind=row.kind.value if hasattr(row.kind, "value") else str(row.kind),
        level=row.level.value if hasattr(row.level, "value") else str(row.level),
        name=row.name,
        message=row.message,
        ts=row.ts,
        ts_ms=row.ts_ms,
        value=row.value,
        unit=row.unit,
        props=row.props or {},
    )


@router.get(
    "/events",
    response_model=EventListResponse,
    summary="List events (paginated + filterable)",
)
async def list_events(
    vendor: str | None = Query(default=None),
    device_name: str | None = Query(default=None),
    device_version: str | None = Query(default=None),
    device_id: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    kind: list[EventKind] | None = Query(default=None),
    level: list[EventLevel] | None = Query(default=None),
    since: datetime | None = Query(default=None, description="Inclusive lower bound on ts"),
    until: datetime | None = Query(default=None, description="Exclusive upper bound on ts"),
    q: str | None = Query(default=None, description="Substring match against name + message"),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> EventListResponse:
    where_clauses = []
    if vendor:
        where_clauses.append(Event.vendor == vendor)
    if device_name:
        where_clauses.append(Event.device_name == device_name)
    if device_version:
        where_clauses.append(Event.device_version == device_version)
    if device_id:
        where_clauses.append(Event.device_id == device_id)
    if session_id:
        where_clauses.append(Event.session_id == session_id)
    if kind:
        where_clauses.append(Event.kind.in_(kind))
    if level:
        where_clauses.append(Event.level.in_(level))
    if since is not None:
        where_clauses.append(Event.ts >= since)
    if until is not None:
        where_clauses.append(Event.ts < until)
    if q:
        # ILIKE substring match -- fast enough for the volumes we're at; we
        # can swap to pg_trgm + a GIN index if /v1/events?q= becomes hot.
        like = f"%{q}%"
        where_clauses.append(or_(Event.name.ilike(like), Event.message.ilike(like)))

    base = select(Event)
    if where_clauses:
        for c in where_clauses:
            base = base.where(c)

    count_stmt = select(func.count()).select_from(base.subquery())
    total = (await session.execute(count_stmt)).scalar_one()

    order_col = desc(Event.ts) if order == "desc" else Event.ts
    rows_stmt = base.order_by(order_col).limit(limit).offset(offset)
    rows = (await session.execute(rows_stmt)).scalars().all()

    return EventListResponse(
        items=[_row_to_out(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


# NOTE: /events/_facets must be declared BEFORE /events/{event_id} or the
# path-param route catches "_facets" as a UUID candidate and returns 400.
@router.get("/events/_facets", summary="Distinct values for filter dropdowns")
async def event_facets(
    since_hours: int = Query(default=24 * 7, ge=1, le=24 * 90),
    session: AsyncSession = Depends(get_session),
) -> dict[str, list[str]]:
    """Return distinct vendor / device_name / device_version values seen in
    the recent window so the UI can populate its filter dropdowns without
    a full table scan."""
    from datetime import timedelta, timezone as tz
    since = datetime.now(tz.utc) - timedelta(hours=since_hours)

    async def _distinct(col):
        stmt = select(col).where(Event.ts >= since).distinct().order_by(col)
        rows = (await session.execute(stmt)).scalars().all()
        return [r for r in rows if r]

    return {
        "vendor": await _distinct(Event.vendor),
        "device_name": await _distinct(Event.device_name),
        "device_version": await _distinct(Event.device_version),
        "device_id": await _distinct(Event.device_id),
    }


@router.get("/events/{event_id}", response_model=EventOut, summary="Fetch a single event")
async def get_event(
    event_id: str,
    session: AsyncSession = Depends(get_session),
) -> EventOut:
    try:
        eid = uuid.UUID(event_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="event_id must be a UUID",
        ) from exc
    row = (
        await session.execute(select(Event).where(Event.id == eid))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    return _row_to_out(row)
