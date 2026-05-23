"""Pydantic schemas mirroring the bz.telemetry wire contract.

The contract (see telemetry_core.cpp::serialize_event + worker_main):

    {
      "vendor":         str,
      "device_name":    str,
      "device_version": str,
      "device_id":      str,        // uuid v4
      "session_id":     str,        // uuid v4
      "user_id":        str,
      "platform":       str,        // "macOS 14.5.0"
      "max_version":    str,
      "type":           "event"|"error"|"metric"|"crash",
      "level":          "info"|"warning"|"error"|"fatal",
      "name":           str,
      "ts":             str,        // ISO8601 UTC, ".000Z"
      "ts_ms":          int,        // unix ms
      "message":        str?,
      "value":          number?,    // metric only
      "unit":           str?,       // metric only
      "props":          {str: str}? // optional bag
    }

We are LIBERAL on input (the device may add fields in future versions and we
shouldn't 422 a Live session over it) and STRICT on output (so consumers of
/v1/stats can rely on the shape).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class IngestEvent(BaseModel):
    """One ndjson line from bz.telemetry."""

    model_config = ConfigDict(extra="ignore")

    # Envelope
    vendor: str = Field(min_length=1, max_length=64)
    device_name: str = Field(default="", max_length=128)
    device_version: str = Field(default="", max_length=64)
    device_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(default="", max_length=256)
    platform: str = Field(default="", max_length=64)
    max_version: str = Field(default="", max_length=128)

    # Event
    type: Literal["event", "error", "metric", "crash"]
    level: Literal["info", "warning", "error", "fatal"] = "info"
    name: str = Field(min_length=1, max_length=256)
    message: str | None = Field(default=None, max_length=10_000)
    ts: datetime
    ts_ms: int

    value: float | None = None
    unit: str | None = Field(default=None, max_length=32)
    props: dict[str, str] = Field(default_factory=dict)

    @field_validator("props", mode="before")
    @classmethod
    def coerce_props(cls, v: Any) -> dict[str, str]:
        # bz.telemetry always sends string-string, but a hand-rolled client
        # might send numbers.  Stringify to keep the schema simple.
        if v is None:
            return {}
        if isinstance(v, dict):
            return {str(k): str(val) for k, val in v.items()}
        raise TypeError("props must be an object")


class IngestResponse(BaseModel):
    accepted: int
    rejected: int
    errors: list[str] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    db: Literal["ok", "down"]
    version: str


class StatsBucket(BaseModel):
    bucket: datetime
    count: int


class VendorStats(BaseModel):
    vendor: str
    device_name: str
    device_version: str
    total: int
    crashes: int
    errors: int
    last_seen: datetime | None


class EventOut(BaseModel):
    """Read-side projection of a stored event row."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    received_at: datetime
    vendor: str
    device_name: str
    device_version: str
    device_id: str
    session_id: str
    user_id: str
    platform: str
    max_version: str
    kind: str
    level: str
    name: str
    message: str | None
    ts: datetime
    ts_ms: int
    value: float | None
    unit: str | None
    props: dict[str, Any]


class EventListResponse(BaseModel):
    items: list[EventOut]
    total: int
    limit: int
    offset: int
