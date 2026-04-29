"""Postgres-backed fixed-window rate limit counter.

The signup / reset / magic-link endpoints don't yet have Redis (and
forcing a Redis dep on community installs is the wrong trade), so the
counter lives in Postgres on the ``rate_limit_counters`` table created
by migration 0077.

Window semantics: fixed minute windows via
``date_trunc('minute', now())``. Boundary-burst risk (a caller can
make 2x ``limit`` requests inside one second across a window edge) is
acceptable for the abuse-control use case here.

The table is intentionally not multi-tenant (no RLS) — limits are
applied pre-auth against IP / email, not against a tenant identity.
We use the schema-owner connection (``AsyncSessionLocal``) so RLS
bypass is implicit.

Public surface:

* ``consume(session, scope_key, limit_per_minute) -> (allowed, count)``
* ``rate_limit(name, limit_per_minute)`` — FastAPI dep factory keyed
  on the request's client IP.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.db import AsyncSessionLocal

logger = logging.getLogger("saebooks.rate_limit")

# Postgres UPSERT: bump count if (scope_key, window_start) row exists,
# else insert with count=1. RETURNING gets the new count back so the
# caller can format the 429 ``Retry-After`` hint.
_UPSERT_SQL = text(
    """
    INSERT INTO rate_limit_counters (scope_key, window_start, count)
    VALUES (:k, date_trunc('minute', now()), 1)
    ON CONFLICT (scope_key, window_start)
    DO UPDATE SET count = rate_limit_counters.count + 1
    RETURNING count
    """
)


async def consume(
    session: AsyncSession,
    scope_key: str,
    limit_per_minute: int,
) -> tuple[bool, int]:
    """Increment the counter for ``scope_key`` in the current minute.

    Returns ``(allowed, new_count)``. ``allowed`` is True iff the new
    count is <= ``limit_per_minute``. The caller decides whether to
    raise — this lets the helper be reused both for hard 429 paths
    and "always-200, but skip the work" paths (eg reset-password
    request, which must be enumeration-safe).
    """
    result = await session.execute(_UPSERT_SQL, {"k": scope_key})
    new_count = int(result.scalar_one())
    await session.commit()
    return (new_count <= limit_per_minute, new_count)


def _client_ip(request: Request) -> str:
    """Best-effort caller IP for the limiter scope.

    Honours ``X-Forwarded-For`` first hop when present (we run behind
    Caddy), otherwise falls back to ``request.client.host`` and "0.0.0.0"
    for the truly headless case (testclient direct ASGI).
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",", 1)[0].strip() or "0.0.0.0"
    if request.client is not None:
        return request.client.host or "0.0.0.0"
    return "0.0.0.0"


def rate_limit(
    name: str, limit_per_minute: int
) -> Callable[[Request], Awaitable[None]]:
    """Build a FastAPI dependency that enforces ``limit_per_minute``
    requests per IP per minute on the named scope.

    Usage::

        @router.post(
            "/signup",
            dependencies=[Depends(rate_limit("signup", 5))],
        )
        async def signup(...): ...
    """

    async def _dep(request: Request) -> None:
        ip = _client_ip(request)
        scope_key = f"{name}:{ip}"
        async with AsyncSessionLocal() as session:
            allowed, count = await consume(session, scope_key, limit_per_minute)
        if not allowed:
            logger.info(
                "rate_limit: %s blocked for %s (count=%d, limit=%d)",
                name,
                ip,
                count,
                limit_per_minute,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests — try again in a minute",
                headers={"Retry-After": "60"},
            )

    return _dep


__all__ = ["consume", "rate_limit"]
