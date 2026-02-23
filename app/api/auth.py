"""API key authentication for sensitive endpoints.

In development mode with no API key configured, auth is bypassed.
In production, all admin and trade endpoints require a valid API key
via the X-API-Key header.
"""

import logging

from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader

from app.config import settings

logger = logging.getLogger(__name__)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(api_key: str | None = Security(_api_key_header)) -> str:
    """Dependency that enforces API key authentication.

    Bypassed in development mode when no API key is configured.
    """
    # If no API key is configured and we're in dev mode, allow access
    if not settings.api_key and settings.app_env == "development":
        return "dev-bypass"

    if not settings.api_key:
        logger.warning("API key not configured but app_env=%s — blocking request", settings.app_env)
        raise HTTPException(status_code=403, detail="API key not configured on server")

    if not api_key or api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    return api_key
