"""Bearer-token auth for the v1 API — Phase 0 dev wiring.

Reads the token from the ``SAEBOOKS_DEV_API_TOKEN`` env var. If the
var is unset, we generate a per-process random token at import time so
running the server without explicit config gives a secure default
(rather than silently accepting any bearer). The random value is
logged at INFO so a developer running the POC script can grab it from
the server log.

Multi-tenant wiring (P0 cross-tenant leak fix)
----------------------------------------------
After the bearer is verified, ``require_bearer`` decodes the JWT (when
present) and stamps the claims onto ``request.state.jwt_claims``. The
shared session dependency (``saebooks.api.v1.deps.get_session``) reads
those claims and issues ``SET LOCAL app.current_tenant`` on the one
session it yields per request, so every query the handler runs is
bound by the ``tenant_isolation`` RLS policy.

``resolve_tenant_id`` reads the JWT claim from ``request.state``
(falling back to the static dev env var only when ``SAEBOOKS_ENV=dev``)
so handlers that still need the raw tenant id for explicit filtering
get the request's tenant — never the historical hard-coded default.
"""
from __future__ import annotations

import logging
import os
import secrets
import uuid

from fastapi import Depends, Header, HTTPException, Request, status

logger = logging.getLogger("saebooks.api.auth")

_ENV_VAR = "SAEBOOKS_DEV_API_TOKEN"
_TENANT_ENV_VAR = "SAEBOOKS_DEV_TENANT_ID"
_DEV_ENV_GUARD = "SAEBOOKS_ENV"

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


def _is_dev_env() -> bool:
    """True when the process is in a dev/test environment.

    Used to guard the env-var tenant override so a misconfigured prod
    container can never silently fall back to the historical default
    tenant. ``pytest`` always sets ``SAEBOOKS_ENV=test``? No — it
    doesn't, by default. We accept any of ``dev``, ``test``,
    ``development``, ``testing`` (case-insensitive) for the override.
    """
    raw = os.environ.get(_DEV_ENV_GUARD, "").strip().lower()
    return raw in {"dev", "test", "development", "testing"}


def resolve_tenant_id(request: Request | None = None) -> uuid.UUID:
    """Resolve the tenant UUID for the current request.

    Preference order:

    1. ``request.state.jwt_claims["tenant_id"]`` — set by
       ``require_bearer`` after decoding the JWT.
    2. ``SAEBOOKS_DEV_TENANT_ID`` env var — only honoured when
       ``SAEBOOKS_ENV`` indicates a dev/test environment, so prod
       can't silently leak into the default tenant when the JWT is
       missing.
    3. Hard-coded default UUID — only as a final fallback in dev/test.

    Raises ``HTTPException(401)`` outside dev/test if neither the JWT
    nor a request-state claim is present.
    """
    if request is not None:
        claims = getattr(request.state, "jwt_claims", None)
        if claims and "tenant_id" in claims:
            try:
                return uuid.UUID(str(claims["tenant_id"]))
            except (ValueError, TypeError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="JWT tenant_id is not a valid UUID",
                ) from exc

    if _is_dev_env():
        raw = os.environ.get(_TENANT_ENV_VAR, "").strip()
        if raw:
            try:
                return uuid.UUID(raw)
            except ValueError:
                logger.warning(
                    "Invalid %s value '%s'; using default tenant",
                    _TENANT_ENV_VAR,
                    raw,
                )
        return DEFAULT_TENANT_ID

    # Production with no JWT claim — refuse to guess the tenant.
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No tenant on request — JWT missing tenant_id claim",
    )


async def require_bearer(
    request: Request,
    authorization: str | None = Header(default=None),
) -> str:
    """FastAPI dependency: enforce Bearer auth on every v1 endpoint.

    Accepts either a JWT issued by POST /auth/login or the static
    SAEBOOKS_DEV_API_TOKEN (backward-compat for scripts and tests).

    On success, when the bearer is a JWT, stamps the decoded claims
    onto ``request.state.jwt_claims`` so ``get_session`` /
    ``resolve_tenant_id`` can read the tenant.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    presented = authorization.split(None, 1)[1].strip()

    # Accept valid JWTs issued by /auth/login.
    from saebooks.services.jwt_tokens import JWTError, decode_access_token  # noqa: PLC0415
    try:
        claims = decode_access_token(presented)
        # Stamp the claims onto request.state so the session dep and
        # downstream handlers can see the tenant. Old code decoded and
        # discarded the claims — this was bug #3 in the leak diagnosis.
        request.state.jwt_claims = claims
        return presented
    except JWTError:
        pass

    # Fall back to static dev token (scripts, tests, direct API access).
    expected = current_token()
    if not secrets.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Static-bearer path — no JWT claims. In dev/test the
    # SAEBOOKS_DEV_TENANT_ID env var (or hard-coded default) provides
    # the tenant. We synthesise a minimal claims dict here so the
    # session dep doesn't have a special case.
    if _is_dev_env():
        request.state.jwt_claims = {"tenant_id": str(resolve_tenant_id(None))}
    return presented


BearerDep = Depends(require_bearer)
