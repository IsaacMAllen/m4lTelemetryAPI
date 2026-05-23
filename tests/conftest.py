"""Pytest configuration.

These tests exercise the wire-contract layer (ndjson parsing + Pydantic
schema validation) without touching a database, because the production
schema is Postgres-specific (JSONB, native enums) and we don't want to
maintain a parallel SQLite-compatible model graph just for tests.

For end-to-end integration testing, run::

    docker compose up --build
    curl -X POST -H 'Content-Type: application/x-ndjson' \\
         --data-binary @tests/fixtures/sample.ndjson \\
         http://localhost:8080/v1/events
"""

from __future__ import annotations

import pytest


@pytest.fixture
def sample_event() -> dict:
    """One event matching the bz.telemetry contract verbatim."""
    return {
        "vendor": "bugbytz",
        "device_name": "livesaver",
        "device_version": "2.0.4",
        "device_id": "11111111-2222-4333-8444-555555555555",
        "session_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        "user_id": "",
        "platform": "macOS 14.5.0",
        "max_version": "Live 12.0.10",
        "type": "event",
        "level": "info",
        "name": "device_loaded",
        "ts": "2026-05-23T15:00:00.000Z",
        "ts_ms": 1779994800000,
        "props": {"slot": "3"},
    }
