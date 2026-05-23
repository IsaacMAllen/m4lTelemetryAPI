"""Wire-contract tests.

These verify that the API-side parser + schema agree with what bz.telemetry
emits.  If you change either side of the contract you should be required to
update these tests in the same commit.
"""

from __future__ import annotations

import orjson
import pytest

from app.ndjson import parse_ndjson
from app.schemas import IngestEvent


def test_ndjson_parses_one_line(sample_event: dict) -> None:
    body = orjson.dumps(sample_event)
    objs, errors = parse_ndjson(body)
    assert errors == []
    assert objs == [sample_event]


def test_ndjson_parses_multiple_lines(sample_event: dict) -> None:
    body = b"\n".join([orjson.dumps(sample_event), orjson.dumps(sample_event)])
    objs, errors = parse_ndjson(body)
    assert errors == []
    assert len(objs) == 2


def test_ndjson_skips_blank_lines() -> None:
    body = b"\n\n\n"
    objs, errors = parse_ndjson(body)
    assert objs == []
    assert errors == []


def test_ndjson_reports_bad_line() -> None:
    body = b'{"valid": 1}\nthis-is-not-json\n{"valid":2}'
    objs, errors = parse_ndjson(body)
    assert len(objs) == 2
    assert len(errors) == 1
    assert "line 2" in errors[0]


def test_schema_accepts_minimum_event() -> None:
    minimal = {
        "vendor": "bugbytz",
        "device_id": "d",
        "session_id": "s",
        "type": "event",
        "name": "boot",
        "ts": "2026-01-01T00:00:00.000Z",
        "ts_ms": 1735689600000,
    }
    ev = IngestEvent.model_validate(minimal)
    assert ev.level == "info"
    assert ev.props == {}


def test_schema_accepts_metric_with_unit_and_value(sample_event: dict) -> None:
    sample_event["type"] = "metric"
    sample_event["value"] = 0.0125
    sample_event["unit"] = "ms"
    ev = IngestEvent.model_validate(sample_event)
    assert ev.value == pytest.approx(0.0125)
    assert ev.unit == "ms"


def test_schema_accepts_crash(sample_event: dict) -> None:
    sample_event["type"] = "crash"
    sample_event["level"] = "fatal"
    sample_event["message"] = "SIGSEGV"
    sample_event["props"] = {"reason": "SIGSEGV", "stack": "0x1234"}
    ev = IngestEvent.model_validate(sample_event)
    assert ev.message == "SIGSEGV"
    assert ev.props == {"reason": "SIGSEGV", "stack": "0x1234"}


def test_schema_rejects_unknown_kind(sample_event: dict) -> None:
    sample_event["type"] = "not-a-kind"
    with pytest.raises(Exception):
        IngestEvent.model_validate(sample_event)


def test_schema_rejects_missing_vendor(sample_event: dict) -> None:
    del sample_event["vendor"]
    with pytest.raises(Exception):
        IngestEvent.model_validate(sample_event)


def test_schema_ignores_unknown_fields(sample_event: dict) -> None:
    """Forward compat: a future device emitting new envelope fields should
    NOT 422 the entire batch."""
    sample_event["future_field"] = "not_in_v1"
    ev = IngestEvent.model_validate(sample_event)
    assert ev.vendor == sample_event["vendor"]


def test_schema_coerces_numeric_props_to_strings(sample_event: dict) -> None:
    sample_event["props"] = {"count": 42, "ratio": 0.5}
    ev = IngestEvent.model_validate(sample_event)
    assert ev.props == {"count": "42", "ratio": "0.5"}
