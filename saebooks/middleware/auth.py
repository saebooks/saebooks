"""JWT auth middleware — resolve user identity from a bearer token.

SAE Books authenticates every browser session through a JWT bearer
token. Tokens are issued by the OAuth login flow (GitHub / Google /
Microsoft) or by the email + password / magic link endpoints under
``/api/v1/auth/``. ``saebooks-web`` carries the session JWT as an
``Authorization: Bearer <jwt>`` header on every internal call to the
HTML routes here.

This middleware:

* reads ``Authorization: Bearer <jwt>``, decodes + verifies it
* looks up the ``User`` row by ``sub`` claim
* stamps ``request.state.user`` (User ORM row), ``request.state.role``,
  ``request.state.username``, and ``request.state.jwt_claims`` for
  downstream FastAPI deps

Fail modes:

* If the header is missing *and* the request is to a gated route, the
  ``require_role`` dep 403s — **not** the middleware. This keeps
  ``/healthz``, ``/metrics``, static files etc. open even when run
  without a session.
* If the JWT is malformed / expired / unverifiable, the request is
  served anonymously — the gate dep handles the actual rejection.
* If the DB lookup fails (Postgres down, migration missing), the
  middleware logs + serves the request anonymously rather than 500.

Dev / test override: set ``SAEBOOKS_DEV_USER=<username>`` and
``SAEBOOKS_DEV_ROLE=<role>`` to upsert a synthetic user and bypass the
JWT decode step entirely. Useful for pytest + local uvicorn without
going through an OAuth dance.
"""
from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from saebooks.db import AsyncSessionLocal
from saebooks.models.user import VALID_ROLES, User, UserRole

logger = logging.getLogger("saebooks.auth")


def _bootstrap_admins() -> frozenset[str]:
    """Usernames that get auto-promoted to ``admin`` on dev-override upsert.

    Comma-separated list in ``SAEBOOKS_BOOTSTRAP_ADMINS``. Solves the
    chicken-and-egg of "first user on a fresh install needs to be admin
    so they can promote others, but the dev override defaults everyone
    to readonly". Once a bootstrap admin has been upserted, they keep
    admin across requests even if removed from the env var (the role is
    stored in the DB, not re-derived each hit).

    The explicit /admin/users flow is the long-term source of truth —
    this env var is a boot knob for fresh dev databases only.
    """

    raw = os.environ.get("SAEBOOKS_BOOTSTRAP_ADMINS", "")
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


# Routes we deliberately serve unauthenticated — healthchecks, metrics,
# static files, public webhooks. Anything else that wants to be
# anonymous-friendly can match a prefix in this tuple.
OPEN_PATH_PREFIXES: tuple[str, ...] = (
    "/healthz",
    "/health",
    "/metrics",
    "/static/",
    "/webhooks/",
    "/favicon.ico",
    # JSON API — uses its own bearer-token auth dependency
    # (``saebooks.api.v1.auth.require_bearer``); doesn't need this
    # middleware to run on every JSON call.
    "/api/",
)


def _is_open_path(path: str) -> bool:
    return any(path.startswith(p) for p in OPEN_PATH_PREFIXES)


# Test-only knob: when set, middleware accepts a ``Remote-User`` header
# as the identity for the current request and upserts a synthetic user
# (same shape as the SAEBOOKS_DEV_USER path). Lets the existing pytest
# suite simulate "user X is signed in for this call" without minting
# a real JWT for every fixture. Never set in production — the prod
# path is JWT-bearer only.
_TEST_HEADER_ENV = "SAEBOOKS_TEST_TRUSTED_USER_HEADER"


def _test_trusted_header_enabled() -> bool:
    return os.environ.get(_TEST_HEADER_ENV) == "1"


def _dev_override() -> tuple[str | None, str | None]:
    """Return (username, role) from env, or (None, None) if unset."""
    u = os.environ.get("SAEBOOKS_DEV_USER") or None
    r = os.environ.get("SAEBOOKS_DEV_ROLE") or None
    if r is not None and r not in VALID_ROLES:
        logger.warning("SAEBOOKS_DEV_ROLE=%r is not a valid role; ignoring", r)
        r = None
    return u, r


async def _upsert_dev_user(
    username: str,
    *,
    dev_role: str | None,
) -> User | None:
    """Idempotent insert-or-update for the dev-override path only.

    Production users are created through the OAuth + signup flows under
    ``/api/v1/auth/``; this helper exists so ``SAEBOOKS_DEV_USER`` works
    against a fresh database without the operator hand-rolling a row.
    Returns the refreshed ORM row or ``None`` if the DB call failed.
    """
    bootstrap_admins = _bootstrap_admins()
    try:
        async with AsyncSessionLocal() as session:
            existing = (
                await session.execute(
                    select(User).where(User.username == username)
                )
            ).scalar_one_or_none()

            now = datetime.now(UTC)
            if existing is None:
                if dev_role:
                    role = dev_role
                elif username in bootstrap_admins:
                    role = UserRole.ADMIN.value
                    logger.info(
                        "Bootstrapping %r as admin (SAEBOOKS_BOOTSTRAP_ADMINS)",
                        username,
                    )
                else:
                    role = UserRole.VIEWER.value
                user = User(
                    tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                    username=username,
                    display_name=username,
                    email=None,
                    role=role,
                    last_seen_at=now,
                )
                session.add(user)
                await session.commit()
                await session.refresh(user)
                return user

            existing.last_seen_at = now
            if dev_role and dev_role != existing.role:
                existing.role = dev_role
            elif (
                username in bootstrap_admins
                and existing.role != UserRole.ADMIN.value
            ):
                logger.info(
                    "Re-bootstrapping %r to admin (SAEBOOKS_BOOTSTRAP_ADMINS)",
                    username,
                )
                existing.role = UserRole.ADMIN.value
            await session.commit()
            await session.refresh(existing)
            return existing
    except Exception as exc:  # defensive — log + serve anonymously
        logger.warning("Dev-override user upsert failed for %s: %s", username, exc)
        return None


async def _user_from_jwt_bearer(
    authorization: str,
) -> tuple[User | None, dict[str, object] | None]:
    """Resolve (User, claims) from an ``Authorization: Bearer <jwt>`` header.

    Returns ``(None, None)`` for any malformed / expired / unverifiable
    token. Returns ``(None, claims)`` when the token decodes but the
    ``sub`` claim points at a missing user — callers may still want to
    use the claims (notably ``tenant_id``) even if the user lookup
    failed (the JSON-API tests, for instance, mint JWTs with random
    sub UUIDs).
    """
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None, None
    token = parts[1].strip()

    # Local imports to avoid a circular at module load time.
    from saebooks.services.jwt_tokens import JWTError, decode_access_token

    try:
        claims = decode_access_token(token)
    except JWTError:
        return None, None

    sub = claims.get("sub")
    if not sub:
        return None, claims
    try:
        user_id = uuid.UUID(str(sub))
    except (ValueError, TypeError):
        return None, claims

    try:
        async with AsyncSessionLocal() as session:
            user = await session.get(User, user_id)
    except Exception as exc:  # defensive — DB hiccup shouldn't 500
        logger.warning("JWT user lookup failed for sub=%s: %s", sub, exc)
        return None, claims
    return user, claims


class ForwardAuthMiddleware(BaseHTTPMiddleware):
    """Resolve the JWT-authenticated user and stamp ``request.state``.

    Name is historical — the class now reads Authorization:
    Bearer <jwt> only. Renaming it would churn every test that
    imports it for no functional benefit.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Default: no user. The role gate dep 403s on gated routes when
        # request.state.user is None.
        request.state.user = None
        request.state.role = None
        request.state.username = None
        # Default: no JWT claims either. The HTML routers and the
        # belt-and-braces tenant filtering rely on this being either
        # populated or missing (resolve_tenant_id treats missing as
        # "fall back to env / dev default / 401 in prod").
        if not hasattr(request.state, "jwt_claims"):
            request.state.jwt_claims = None

        if _is_open_path(request.url.path):
            return await call_next(request)

        dev_user, dev_role = _dev_override()
        if dev_user:
            user = await _upsert_dev_user(dev_user, dev_role=dev_role)
            if user is not None and user.archived_at is None:
                request.state.user = user
                request.state.role = user.role
                request.state.username = user.username
                request.state.jwt_claims = {"tenant_id": str(user.tenant_id)}
            elif user is not None and user.archived_at is not None:
                request.state.username = user.username
            return await call_next(request)

        # Test-only trusted-header path. Conftest sets the env var so
        # individual tests can supply Remote-User: <username> rather
        # than mint a JWT each time. NEVER reachable in prod.
        if _test_trusted_header_enabled():
            test_user_header = request.headers.get("remote-user")
            if test_user_header:
                user = await _upsert_dev_user(
                    test_user_header, dev_role=None
                )
                if user is not None and user.archived_at is None:
                    request.state.user = user
                    request.state.role = user.role
                    request.state.username = user.username
                    request.state.jwt_claims = {
                        "tenant_id": str(user.tenant_id)
                    }
                elif user is not None and user.archived_at is not None:
                    request.state.username = user.username
                return await call_next(request)

        authz = request.headers.get("authorization")
        jwt_user: User | None = None
        jwt_claims: dict[str, object] | None = None
        if authz:
            jwt_user, jwt_claims = await _user_from_jwt_bearer(authz)

        # Stamp claims on request.state regardless of user-lookup
        # outcome — the JWT's tenant_id is still authoritative even if
        # ``sub`` doesn't resolve to a User row (e.g. tests that mint
        # synthetic JWTs with random sub UUIDs).
        if jwt_claims is not None:
            request.state.jwt_claims = jwt_claims

        if jwt_user is not None and jwt_user.archived_at is None:
            request.state.user = jwt_user
            request.state.role = jwt_user.role
            request.state.username = jwt_user.username
        else:
            level = (
                logging.INFO
                if request.url.path.startswith("/admin/")
                else logging.DEBUG
            )
            logger.log(
                level,
                "No user identity on %s; saw headers: %s",
                request.url.path,
                sorted(request.headers.keys()),
            )

        return await call_next(request)
