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
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_ingest_token
from ..config import Settings, get_settings
from ..db import get_session
from ..models import Event, EventKind, EventLevel
from ..ndjson import parse_ndjson
from ..schemas import IngestEvent, IngestResponse

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
