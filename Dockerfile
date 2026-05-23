# Multi-stage build:
#   - `builder` installs deps into a virtualenv (no compiler in the final image).
#   - `runtime` is a minimal slim image running as a non-root user.
#
# We pin Python 3.12; pydantic 2 + asyncpg both have wheels for it.

# ---------------------------------------------------------------------------
# Build stage
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /build

# Build deps for asyncpg (only needed if a wheel is missing for the target
# architecture; harmless on amd64 / arm64 where wheels exist).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

# ---------------------------------------------------------------------------
# Runtime stage
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Install minimal runtime libs (libpq is NOT required for asyncpg).  tini gives
# us proper PID-1 signal handling for graceful shutdowns under k8s.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app \
    && useradd  --system --gid app --home /app --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=app:app app ./app
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app alembic.ini .

USER app

EXPOSE 8080

# 8080 because Kubernetes Ingress controllers default-allow it through PSP /
# Pod Security profiles that block ports < 1024 for non-root users.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--no-server-header", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
