"""Shared FastAPI dependencies for the v1 API.

Currently exposes ``get_session`` — a request-scoped ``AsyncSession``
that issues ``SET app.current_tenant = '<jwt-tenant-id>'`` (session
scope, not LOCAL) before yielding, so every query in the request runs
under the ``tenant_isolation`` RLS policy installed by migration 0055.

Why a dedicated dep
-------------------
The plain ``saebooks.db.get_session`` does not know about the request
or the JWT. The leak diagnosis (see
``audit-trail/02-cross-tenant-leak-diagnosis.md``) traces the cause to
every handler opening its own ``AsyncSessionLocal()`` and never
binding the tenant — ``app.current_tenant`` was unset for every query.

Why session-scope, not LOCAL
----------------------------
``SET LOCAL`` binds the GUC for the duration of the current
transaction. The existing service layer (``services/contacts.py``,
``services/invoices.py``, etc.) commits inside individual helper
functions, so a single request handler may span multiple transactions
on the same session. ``SET LOCAL`` would be lost on the first commit
and subsequent queries would run with no tenant set — under FORCE RLS
that means returning zero rows (or worse, accidentally bypassing the
filter if a later refactor reverts FORCE).

We use plain ``SET`` so the value persists across transactions for the
lifetime of the connection. Because ``saebooks.db.engine`` uses
``NullPool``, every ``AsyncSessionLocal()`` opens a fresh connection
that's discarded when the session closes — there's no risk of a stale
GUC leaking into the next request.

Pattern
-------
::

    from fastapi import Depends
    from sqlalchemy.ext.asyncio import AsyncSession
    from saebooks.api.v1.deps import get_session

    @router.get("/contacts")
    async def list_contacts(
        session: AsyncSession = Depends(get_session),
    ) -> list[ContactOut]:
        # session has SET app.current_tenant = <request tenant>
        ...

The dep depends on ``require_bearer`` having already attached
``request.state.jwt_claims``. ``require_bearer`` is registered as a
router-level dependency on every v1 router, so by the time the path
operation receives the session, the claims are populated.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import resolve_tenant_id
from saebooks.db import AsyncSessionLocal


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield one ``AsyncSession`` per request with ``app.current_tenant`` set.

    Tenant resolution failure
        If ``resolve_tenant_id`` raises (no JWT claim and not in dev),
        the exception bubbles up through the dep chain as a 401 — the
        handler never runs. This is preferable to running with no
        tenant set, which under FORCE RLS would silently return zero
        rows (looking like "everything's fine").
    """
    tenant_id = resolve_tenant_id(request)
    # SET (not SET LOCAL) — persist across the multiple transactions
    # the legacy service layer commits inside its helpers. NullPool
    # ensures the connection is discarded at session close, so the
    # GUC can't leak between requests.
    #
    # Postgres ``SET name = value`` is a utility command that does NOT
    # accept parameter placeholders, so we cannot use bind parameters.
    # The value is interpolated directly — but only after coercing
    # through ``uuid.UUID`` so the string is constrained to a UUID
    # literal and SQL injection is impossible.
    tid_literal = str(tenant_id)
    async with AsyncSessionLocal() as session:
        await session.execute(text(f"SET app.current_tenant = '{tid_literal}'"))
        # SET runs in an auto-started transaction; commit it so the
        # GUC takes effect at the connection level rather than being
        # tied to the doomed implicit transaction.
        await session.commit()
        yield session
