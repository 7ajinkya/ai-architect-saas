"""API safety layer: request authentication and CORS configuration.

Local-demo scope. The API binds to the loopback interface by default
(see ``ai_architect.api.__main__``) and requires a bearer token on every
consequential mutating endpoint. Read endpoints and ``/health`` stay
open so the UI can display state.

The token is read from the server process environment at request time
(never cached, never logged, never echoed). When no token is
configured, mutating endpoints fail closed with 503 instead of
accepting unauthenticated writes.
"""

import os
import secrets

from fastapi import Header, HTTPException

API_TOKEN_ENV = "AI_ARCHITECT_API_TOKEN"
CORS_ORIGINS_ENV = "AI_ARCHITECT_CORS_ORIGINS"

# The local dev UI origins (vite dev server). CORS is a browser policy,
# not authentication; it only bounds which pages may call the API.
DEFAULT_CORS_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


def cors_origins() -> list[str]:
    """Allowed browser origins; env override is comma-separated."""
    raw = os.environ.get(CORS_ORIGINS_ENV)
    if raw is None:
        return list(DEFAULT_CORS_ORIGINS)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _presented_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not value:
        return None
    return value


async def require_api_token(
    authorization: str | None = Header(default=None),
) -> None:
    """Guard all mutating endpoints; read at request time, fail closed."""
    configured = os.environ.get(API_TOKEN_ENV, "").strip()
    if not configured:
        raise HTTPException(
            status_code=503,
            detail={
                "detail": (
                    "The API is not accepting mutations: no API token is "
                    "configured on the server. Set AI_ARCHITECT_API_TOKEN "
                    "in the server environment and restart."
                ),
                "code": "api_token_not_configured",
            },
        )
    presented = _presented_token(authorization)
    if presented is None or not secrets.compare_digest(presented, configured):
        raise HTTPException(
            status_code=401,
            detail={
                "detail": "Missing or invalid API token.",
                "code": "unauthorized",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
