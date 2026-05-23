"""Bearer-token authentication for /v1/events.

bz.telemetry sends `Authorization: Bearer <token>` when an `endpoint_token` is
configured on the device.  We accept any token that's listed in
TELEMETRY_INGEST_TOKENS (comma-separated).

If no tokens are configured we accept unauthenticated POSTs — handy for local
dev but you should set tokens via a Kubernetes Secret in production.
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status

from .config import Settings, get_settings


async def require_ingest_token(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    if not settings.auth_required:
        return  # auth disabled for this deployment

    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="expected Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if token not in settings.ingest_token_set:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="invalid ingest token"
        )
