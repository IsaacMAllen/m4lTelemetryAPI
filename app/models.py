"""SQLAlchemy ORM models.

Schema design rationale
-----------------------
We deliberately keep this to ONE table (`events`) with a JSONB `props` column.
Reasons:

  - The bz.telemetry payload is intentionally flat + small.  Splitting it
    into normalised dimension tables adds write amplification for no analytic
    win at our scale (a single device emits at most a few events/sec).
  - JSONB lets product-specific properties evolve without migrations.
  - Crash + error events are first-class via the `kind` enum + `level` enum
    + the `message` column, so dashboards filter cheaply on indexed columns.

Indexes
  - (received_at DESC) — recent firehose for ops dashboards.
  - (vendor, device_name, ts DESC) — per-device drilldown.
  - (kind, level, ts DESC) — global "what's broken right now" view.
  - GIN(props) — ad-hoc filtering on arbitrary keys (rare, but invaluable
    when investigating a bug report).

A future change might partition `events` by month (`PARTITION BY RANGE (ts)`)
once volume justifies it.  Today it's premature.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Float,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class EventKind(str, enum.Enum):
    event = "event"
    error = "error"
    metric = "metric"
    crash = "crash"


class EventLevel(str, enum.Enum):
    info = "info"
    warning = "warning"
    error = "error"
    fatal = "fatal"


class Event(Base):
    """One row per telemetry event delivered by bz.telemetry."""

    __tablename__ = "events"

    # Primary key is server-generated UUIDv4.  We don't trust client ids as
    # primaries (they could collide across devices), but we do store the
    # client-side timestamp for ordering.
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # ---- Server-side audit ---------------------------------------------------
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )

    # ---- Envelope (who / what / where) --------------------------------------
    vendor: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    device_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    device_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    device_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    platform: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    max_version: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    # ---- Event payload ------------------------------------------------------
    kind: Mapped[EventKind] = mapped_column(
        Enum(EventKind, name="event_kind"), nullable=False, index=True
    )
    level: Mapped[EventLevel] = mapped_column(
        Enum(EventLevel, name="event_level"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Client-side timestamp.  ts_ms is the millisecond integer; ts_iso is the
    # human-readable ISO8601 string.  We store both so we can order without
    # re-parsing strings, and have an exact textual record of what the device
    # claimed.
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ts_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Metric-only fields.  Cheaper than a separate metrics table.
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Free-form properties.  JSONB so we can index + query.
    props: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )

    __table_args__ = (
        Index("ix_events_vendor_device_ts", "vendor", "device_name", "ts"),
        Index("ix_events_kind_level_ts", "kind", "level", "ts"),
        Index("ix_events_props_gin", "props", postgresql_using="gin"),
    )
