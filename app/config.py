"""Runtime configuration, sourced from environment variables.

We use pydantic-settings so that:
  - Defaults are declared in one place and self-documenting.
  - Type coercion + validation happens at startup, not at first request.
  - The same Settings object can be injected into FastAPI dependencies for
    test overrides (see tests/conftest.py).

DSN sources (in priority order)
-------------------------------
1. ``TELEMETRY_DATABASE_URL`` — used as-is. This is the local-dev path
   (Homebrew Postgres or docker-compose).
2. ``TELEMETRY_DB_HOST`` + ``_PORT`` + ``_USER`` + ``_PASSWORD`` + ``_NAME``
   — assembled into an asyncpg DSN at startup.  This is the k8s production
   path: each component is `valueFrom`'d from the Secret that
   CloudNativePG creates for the cluster (named ``<cluster>-app``).

All env vars use the TELEMETRY_ prefix so they don't collide with anything
else in a shared cluster.
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import quote_plus

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="TELEMETRY_",
        extra="ignore",
    )

    # ---- Database (Path 1: full DSN) ----------------------------------------
    database_url: str = Field(
        default="",
        description=(
            "SQLAlchemy DSN. Must use the asyncpg driver. "
            "Takes precedence over TELEMETRY_DB_* component vars."
        ),
    )

    # ---- Database (Path 2: components, CNPG-friendly) -----------------------
    db_host: str = Field(default="")
    db_port: int = Field(default=5432)
    db_user: str = Field(default="")
    db_password: str = Field(default="")
    db_name: str = Field(default="")

    # ---- Auth ---------------------------------------------------------------
    ingest_tokens: str = Field(
        default="",
        description="Comma-separated bearer tokens accepted on /v1/events.",
    )

    # ---- Server -------------------------------------------------------------
    log_level: str = Field(default="INFO")
    max_body_bytes: int = Field(default=4 * 1024 * 1024)
    max_events_per_request: int = Field(default=1000)
    cors_origins: str = Field(default="")

    # ---- Convenience accessors ----------------------------------------------
    @property
    def resolved_database_url(self) -> str:
        """Return the DSN, building it from components if necessary.

        We URL-encode the password so passwords containing '@', ':', or '/'
        don't break parsing.  CNPG generates passwords with `+`, `/`, and
        '=' in base64-style alphabets which would otherwise corrupt the URL.
        """
        if self.database_url:
            return self.database_url
        if self.db_host and self.db_user and self.db_name:
            pw = quote_plus(self.db_password)
            user = quote_plus(self.db_user)
            return (
                f"postgresql+asyncpg://{user}:{pw}@"
                f"{self.db_host}:{self.db_port}/{self.db_name}"
            )
        # Last-resort fallback for the very first `make dev` run.
        return "postgresql+asyncpg://telemetry:telemetry@localhost:5432/telemetry"

    @property
    def ingest_token_set(self) -> frozenset[str]:
        return frozenset(t.strip() for t in self.ingest_tokens.split(",") if t.strip())

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def auth_required(self) -> bool:
        return bool(self.ingest_token_set)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton so we don't re-parse env on every request."""
    return Settings()
