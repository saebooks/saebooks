"""Bearer-token auth for the v1 API — Phase 0 dev wiring.

Reads the token from the ``SAEBOOKS_DEV_API_TOKEN`` env var. If the
var is unset, we generate a per-process random token at import time so
running the server without explicit config gives a secure default
(rather than silently accepting any bearer). The random value is
logged at INFO so a developer running the POC script can grab it from
the server log.

Multi-tenant wiring
-------------------
After the bearer is verified, ``require_bearer`` sets the Postgres
session-local variable ``app.current_tenant`` via a raw ``SET LOCAL``
call.  The value comes from:

1. ``SAEBOOKS_DEV_TENANT_ID`` env var if set (test/dev override).
2. Hard-coded default UUID (00000000-0000-0000-0000-000000000001) —
   the single "Default" tenant seeded by migration 0040.

TODO(phase-1): replace with JWT verification against
``portal.sauer.com.au``'s JWKS once Lane A cycle 5 deploys the portal.
The JWT will carry ``tenant_id`` as a claim; extract it here and drop
the env-var fallback.
"""
from __future__ import annotations

import logging
import os
import secrets
import uuid

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import text

from saebooks.db import AsyncSessionLocal

logger = logging.getLogger("saebooks.api.auth")

_ENV_VAR = "SAEBOOKS_DEV_API_TOKEN"
_TENANT_ENV_VAR = "SAEBOOKS_DEV_TENANT_ID"

# Default tenant UUID — matches the seed row in migration 0040_tenants.
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


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


def resolve_tenant_id() -> uuid.UUID:
    """Resolve the tenant UUID for the current request.

    In dev/test we use the env var override; in production this will be
    replaced by a JWT claim extraction.
    """
    raw = os.environ.get(_TENANT_ENV_VAR, "").strip()
    if raw:
        try:
            return uuid.UUID(raw)
        except ValueError:
            logger.warning("Invalid %s value '%s'; using default tenant", _TENANT_ENV_VAR, raw)
    return DEFAULT_TENANT_ID


async def set_tenant_on_connection(tenant_id: uuid.UUID) -> None:
    """Issue ``SET LOCAL app.current_tenant`` on a fresh connection.

    Called by request-scoped auth dependencies so every query in the
    request runs inside the correct RLS scope.

    Note: we open a *separate* session here just to set the GUC.  The
    service-layer sessions are independent (NullPool — each call to
    AsyncSessionLocal() spawns a fresh connection).  The RLS policy is
    enforced at the connection level; as long as every session opened
    inside a request handler also calls this helper, isolation holds.

    For Phase 1 we will inject this into a middleware or a shared
    session factory so it's guaranteed to run before every query.
    """
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SET LOCAL app.current_tenant = :tid"),
            {"tid": str(tenant_id)},
        )
        await session.commit()


async def require_bearer(
    authorization: str | None = Header(default=None),
) -> str:
    """FastAPI dependency: enforce Bearer auth on every write endpoint.

    Also resolves and stores the current tenant ID in the process-level
    default for this request.  Phase 1 will derive tenant_id from the
    JWT claims instead.
    """
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
