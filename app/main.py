"""FastAPI application entrypoint.

Run locally:

    uvicorn app.main:app --reload --port 8080

In Docker / Kubernetes we run uvicorn directly with workers=1; horizontal
scaling is done by adding pods, not by spawning extra worker processes per
pod (cleaner for cluster autoscaler accounting).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pythonjsonlogger import jsonlogger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from . import __version__
from .config import get_settings
from .db import dispose_engine, init_engine
from .routes import events, health, stats


def _configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"asctime": "ts", "levelname": "level", "name": "logger"},
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # Quiet down noisy libraries.
    logging.getLogger("uvicorn.access").setLevel("INFO")
    logging.getLogger("sqlalchemy.engine").setLevel("WARNING")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    _configure_logging(settings.log_level)
    init_engine(settings)
    logging.getLogger(__name__).info(
        "m4l-telemetry-api starting",
        extra={"version": __version__, "auth_required": settings.auth_required},
    )
    try:
        yield
    finally:
        await dispose_engine()


app = FastAPI(
    title="m4l-telemetry-api",
    version=__version__,
    description=(
        "Receiver API for events emitted by the bz.telemetry Max for Live external."
    ),
    default_response_class=ORJSONResponse,
    lifespan=lifespan,
)


# ---- CORS (only if explicitly configured) -----------------------------------
_settings = get_settings()
if _settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_settings.cors_origin_list,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=False,
    )


# ---- Prometheus metrics ------------------------------------------------------
# Lightweight per-route counters.  We keep cardinality low (no path
# templating tricks) to stay friendly to a shared Prometheus.
REQ_COUNT = Counter(
    "telemetry_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)
REQ_LATENCY = Histogram(
    "telemetry_request_duration_seconds",
    "HTTP request latency",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        # Use the matched route path (or the raw path on 404) so we don't blow
        # up label cardinality on dynamic IDs.
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        method = request.method
        with REQ_LATENCY.labels(method=method, path=path).time():
            response = await call_next(request)
        REQ_COUNT.labels(method=method, path=path, status=str(response.status_code)).inc()
        return response


app.add_middleware(MetricsMiddleware)


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---- Routers ----------------------------------------------------------------
app.include_router(health.router)
app.include_router(events.router)
app.include_router(stats.router)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {
        "name": "m4l-telemetry-api",
        "version": __version__,
        "docs": "/docs",
        "ingest": "/v1/events",
        "health": "/healthz",
    }
