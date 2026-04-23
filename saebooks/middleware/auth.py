"""Forward-auth middleware — read user identity from Caddy/Authentik.

SAE Books sits behind Caddy + Authentik forward-auth
(``https://books.sauer.com.au``). Authentik's outpost forwards identity
under two naming schemes:

* ``Remote-User`` / ``Remote-Email`` / ``Remote-Name`` — only when the
  proxy provider's "Return the user as Remote-User header" is enabled.
* ``X-authentik-username`` / ``X-authentik-email`` / ``X-authentik-name``
  — emitted unconditionally on every outpost response.

We accept both. ``Remote-*`` wins when present (explicit opt-in);
``X-authentik-*`` is the fallback. Mirrors the ``copy_headers`` list
in the Caddy ``(authentik)`` snippet.

This middleware:

* reads either header set
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

# Authentik outpost forwards user identity under TWO naming schemes
# depending on configuration. The ``Remote-*`` headers are the
# traditional forward-auth shape (what most docs assume) — Authentik
# only emits those when the proxy provider is configured with
# "Return the user as Remote-User header" turned on. The ``X-authentik-*``
# variants are emitted unconditionally by every Authentik outpost.
#
# We accept both so the middleware Just Works whether the outpost is
# in default mode or the Remote-User-returning mode. When both are
# present (rare), Remote-User wins because it's the one the admin
# explicitly opted into.
REMOTE_USER_HEADER = "remote-user"
REMOTE_EMAIL_HEADER = "remote-email"
REMOTE_NAME_HEADER = "remote-name"
AUTHENTIK_USERNAME_HEADER = "x-authentik-username"
AUTHENTIK_EMAIL_HEADER = "x-authentik-email"
AUTHENTIK_NAME_HEADER = "x-authentik-name"


def _bootstrap_admins() -> frozenset[str]:
    """Usernames that get auto-promoted to ``admin`` on upsert.

    Comma-separated list in ``SAEBOOKS_BOOTSTRAP_ADMINS``. Solves the
    chicken-and-egg of "first user on a fresh install needs to be admin
    so they can promote others, but the middleware defaults everyone to
    readonly". Once a bootstrap admin has been upserted, they keep
    admin across requests even if removed from the env var (the role is
    stored in the DB, not re-derived each hit).

    The explicit /admin/users flow is still the long-term source of
    truth — this env var is just a boot knob.
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
    # (``saebooks.api.v1.auth.require_bearer``); doesn't want the
    # Authentik user upsert on every call.
    "/api/",
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
                # Bootstrap admins always start at admin; dev override
                # wins over bootstrap (useful in tests); everyone else
                # starts readonly and gets promoted via /admin/users.
                if dev_role:
                    role = dev_role
                elif username in bootstrap_admins:
                    role = UserRole.ADMIN.value
                    logger.info(
                        "Bootstrapping %r as admin (SAEBOOKS_BOOTSTRAP_ADMINS)",
                        username,
                    )
                else:
                    role = UserRole.READONLY.value
                user = User(
                    tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
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
            # Bootstrap admins get auto-repaired: if the env lists a
            # username that somehow got demoted below admin (fresh DB
            # seed, manual SQL, etc.), bump them back up on next hit.
            # Once the env var is removed, this stops firing — the role
            # stays wherever /admin/users set it.
            elif username in bootstrap_admins and existing.role != UserRole.ADMIN.value:
                logger.info(
                    "Re-bootstrapping %r to admin (SAEBOOKS_BOOTSTRAP_ADMINS)",
                    username,
                )
                existing.role = UserRole.ADMIN.value
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
        # Prefer Remote-User (explicit forward-auth opt-in), fall back
        # to X-authentik-username (what Authentik outposts emit by
        # default). Same fallback for email + name so the /admin/users
        # list shows the display name even when only the X-authentik-*
        # headers are coming through.
        username = (
            dev_user
            or request.headers.get(REMOTE_USER_HEADER)
            or request.headers.get(AUTHENTIK_USERNAME_HEADER)
        )
        email = (
            request.headers.get(REMOTE_EMAIL_HEADER)
            or request.headers.get(AUTHENTIK_EMAIL_HEADER)
        )
        display = (
            request.headers.get(REMOTE_NAME_HEADER)
            or request.headers.get(AUTHENTIK_NAME_HEADER)
        )

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
        else:
            # No identity headers at all — neither Remote-User nor
            # X-authentik-username. Most of the time this is an
            # anonymous healthcheck-ish request from inside the docker
            # network. On /admin/* paths it means forward-auth isn't
            # wired up right; log at INFO so the misconfig is visible
            # without flipping the global level.
            level = logging.INFO if request.url.path.startswith("/admin/") else logging.DEBUG
            logger.log(
                level,
                "No user identity on %s; saw headers: %s",
                request.url.path,
                sorted(request.headers.keys()),
            )

        return await call_next(request)
