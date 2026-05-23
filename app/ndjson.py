"""ndjson body parser.

We accept `application/x-ndjson` bodies (and tolerate `application/json` for
hand-rolled curl tests).  Each non-empty line is parsed with orjson — which
is roughly 3× faster than the stdlib json module on telemetry-shaped payloads.

We surface bad lines as soft errors (returned in the IngestResponse.errors
field) rather than failing the whole batch, because:

  - bz.telemetry retries the entire batch on any non-2xx response.
  - One malformed row would otherwise wedge the queue forever.
  - Operators can see the issue in IngestResponse.errors without trawling
    server logs.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import orjson


def iter_ndjson_lines(raw: bytes) -> Iterator[tuple[int, bytes]]:
    """Yield (line_number_starting_at_1, line_bytes) for each non-empty line."""
    lineno = 0
    for chunk in raw.splitlines():
        lineno += 1
        s = chunk.strip()
        if not s:
            continue
        yield lineno, s


def parse_ndjson(raw: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (parsed_objects, error_messages).

    Errors are formatted as ``"line N: <reason>"`` so an operator can locate
    the bad event in the request body.
    """
    out: list[dict[str, Any]] = []
    errors: list[str] = []
    for lineno, line in iter_ndjson_lines(raw):
        try:
            obj = orjson.loads(line)
        except orjson.JSONDecodeError as exc:
            errors.append(f"line {lineno}: invalid JSON ({exc})")
            continue
        if not isinstance(obj, dict):
            errors.append(f"line {lineno}: top-level value must be an object")
            continue
        out.append(obj)
    return out, errors
