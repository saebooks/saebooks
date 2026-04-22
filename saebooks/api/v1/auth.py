"""Bearer-token auth for the v1 API — Phase 0 dev wiring.

Reads the token from the ``SAEBOOKS_DEV_API_TOKEN`` env var. If the
var is unset, we generate a per-process random token at import time so
running the server without explicit config gives a secure default
(rather than silently accepting any bearer). The random value is
logged at INFO so a developer running the POC script can grab it from
the server log.

TODO(phase-1): replace with JWT verification against
``portal.sauer.com.au``'s JWKS once Lane A cycle 5 deploys the portal.
"""
from __future__ import annotations

import logging
import os
import secrets

from fastapi import Depends, Header, HTTPException, status

logger = logging.getLogger("saebooks.api.auth")

_ENV_VAR = "SAEBOOKS_DEV_API_TOKEN"


def _resolve_token() -> str:
    token = os.environ.get(_ENV_VAR, "").strip()
    if token:
        return token
    # Generate once per process; log so dev can grab it.
    generated = secrets.token_urlsafe(32)
    logger.info(
        "%s not set; using ephemeral dev token (pass as 'Authorization: Bearer %s')",
        _ENV_VAR,
        generated,
    )
    os.environ[_ENV_VAR] = generated
    return generated


_TOKEN = _resolve_token()


def current_token() -> str:
    """Return the process-wide expected bearer token (testing hook)."""
    return os.environ.get(_ENV_VAR, _TOKEN)


async def require_bearer(
    authorization: str | None = Header(default=None),
) -> str:
    """FastAPI dependency: enforce Bearer auth on every write endpoint."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    presented = authorization.split(None, 1)[1].strip()
    expected = current_token()
    if not secrets.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return presented


BearerDep = Depends(require_bearer)
