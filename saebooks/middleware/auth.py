"""Forward-auth middleware — read ``Remote-User`` from Caddy/Authentik.

SAE Books sits behind Caddy + Authentik forward-auth
(``https://books.sauer.com.au``). Authentik sets a ``Remote-User``
header (plus ``Remote-Email`` and ``Remote-Name``) on every proxied
request once the SSO check passes.

This middleware:

* reads the three headers
* upserts a row in ``users`` (keyed on username) so the admin UI can
  see every human who's ever reached the app
* stamps ``request.state.user`` (``User`` ORM row) and
  ``request.state.role`` (str) for downstream FastAPI deps

Fail modes:

* If the header is missing *and* the request is to a gated route, the
  ``require_role`` dep 403s — **not** the middleware. This keeps
  ``/healthz``, ``/metrics``, static files etc. open even when run
  outside forward-auth (e.g. local dev, smoke tests).
* If the DB upsert fails (e.g. migration hasn't run yet), the
  middleware logs + serves the request anonymously — never 500s just
  because the users table is missing.

Dev / test override: set ``SAEBOOKS_DEV_USER=<username>`` and
``SAEBOOKS_DEV_ROLE=<role>`` to bypass the header read. Useful for
pytest + local uvicorn without Authentik in front.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from saebooks.db import AsyncSessionLocal
from saebooks.models.user import VALID_ROLES, User, UserRole

logger = logging.getLogger("saebooks.auth")

REMOTE_USER_HEADER = "remote-user"
REMOTE_EMAIL_HEADER = "remote-email"
REMOTE_NAME_HEADER = "remote-name"

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
)


def _is_open_path(path: str) -> bool:
    return any(path.startswith(p) for p in OPEN_PATH_PREFIXES)


def _dev_override() -> tuple[str | None, str | None]:
    """Return (username, role) from env, or (None, None) if unset."""
    u = os.environ.get("SAEBOOKS_DEV_USER") or None
    r = os.environ.get("SAEBOOKS_DEV_ROLE") or None
    if r is not None and r not in VALID_ROLES:
        logger.warning("SAEBOOKS_DEV_ROLE=%r is not a valid role; ignoring", r)
        r = None
    return u, r


async def _upsert_user(
    username: str,
    *,
    email: str | None,
    display_name: str | None,
    dev_role: str | None = None,
) -> User | None:
    """Idempotent insert-or-update keyed on ``username``.

    Returns the refreshed ORM row or ``None`` if the DB call failed
    (table missing, Postgres down, …) — caller treats that as "serve
    anonymously" rather than crash.
    """
    try:
        async with AsyncSessionLocal() as session:
            existing = (
                await session.execute(
                    select(User).where(User.username == username)
                )
            ).scalar_one_or_none()

            now = datetime.now(UTC)
            if existing is None:
                role = dev_role or UserRole.READONLY.value
                user = User(
                    username=username,
                    display_name=display_name,
                    email=email,
                    role=role,
                    last_seen_at=now,
                )
                session.add(user)
                await session.commit()
                await session.refresh(user)
                return user

            # Touch last_seen, refresh optional profile fields if the
            # upstream now knows the email/name.
            existing.last_seen_at = now
            if email and not existing.email:
                existing.email = email
            if display_name and not existing.display_name:
                existing.display_name = display_name
            # Dev override can escalate role on every request; production
            # never touches role here — admin-only UI does it.
            if dev_role and dev_role != existing.role:
                existing.role = dev_role
            await session.commit()
            await session.refresh(existing)
            return existing
    except Exception as exc:  # defensive — log + serve anonymously
        logger.warning("User upsert failed for %s: %s", username, exc)
        return None


class ForwardAuthMiddleware(BaseHTTPMiddleware):
    """Attach the Authentik-authenticated user to ``request.state``."""

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

        if _is_open_path(request.url.path):
            return await call_next(request)

        dev_user, dev_role = _dev_override()
        username = dev_user or request.headers.get(REMOTE_USER_HEADER)
        email = request.headers.get(REMOTE_EMAIL_HEADER)
        display = request.headers.get(REMOTE_NAME_HEADER)

        if username:
            user = await _upsert_user(
                username,
                email=email,
                display_name=display,
                dev_role=dev_role,
            )
            if user is not None and user.archived_at is None:
                request.state.user = user
                request.state.role = user.role
                request.state.username = user.username
            elif user is not None and user.archived_at is not None:
                # Archived users stay authenticated-but-powerless —
                # username logged, but role is None so every gate
                # 403s. Prevents an admin "removing" a user from
                # racing with an in-flight request.
                request.state.username = user.username

        return await call_next(request)
