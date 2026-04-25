"""Shared FastAPI dependencies for the v1 API.

Currently exposes ``get_session`` — a request-scoped ``AsyncSession``
that issues ``SET LOCAL app.current_tenant = '<jwt-tenant-id>'`` at
the start of every transaction, so every query in the request runs
under the ``tenant_isolation`` RLS policy installed by migration 0055.

Why a dedicated dep
-------------------
The plain ``saebooks.db.get_session`` does not know about the request
or the JWT. The leak diagnosis (see
``audit-trail/02-cross-tenant-leak-diagnosis.md``) traces the cause to
every handler opening its own ``AsyncSessionLocal()`` and never
binding the tenant — ``app.current_tenant`` was unset for every query.

Why an after_begin event hook
-----------------------------
The naive implementation — run ``SET app.current_tenant = '...'`` once
at session open and rely on session-level scoping — is broken in
practice. SQLAlchemy + asyncpg + NullPool combine to release the
underlying connection on every ``session.commit()``; the next query
acquires a fresh connection and the session-level GUC is gone. The
service layer commits inside individual helpers so this happens many
times per request.

The robust fix is to re-issue ``SET LOCAL app.current_tenant = '...'``
inside every transaction. We hang the tenant id off
``session.info['tenant_id']`` and install one process-wide
``after_begin`` listener on the synchronous ``Session`` class. The
listener inspects ``session.info`` — if a tenant is present, it
issues the SET LOCAL on the just-begun transaction. Sessions with no
tenant in ``info`` are untouched (so legacy Jinja code paths and
admin tooling keep working).

The interpolation is safe: ``resolve_tenant_id`` returns a
``uuid.UUID`` so the string is constrained to a UUID literal — SQL
injection is impossible. ``SET`` does not accept bind parameters,
which is why we have to interpolate.

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
        # every query under this session has app.current_tenant set
        ...

The dep depends on ``require_bearer`` having already attached
``request.state.jwt_claims``. ``require_bearer`` is registered as a
router-level dependency on every v1 router, so by the time the path
operation receives the session, the claims are populated.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from saebooks.api.v1.auth import resolve_tenant_id
from saebooks.db import AsyncSessionLocal


def _set_current_tenant_on_begin(
    session: Session,
    transaction: object,
    connection: object,
) -> None:
    """SQLAlchemy ``after_begin`` listener — re-binds the tenant GUC.

    Reads ``session.info['tenant_id']`` (set by ``get_session`` or the
    test fixture). When present, runs ``SET LOCAL app.current_tenant
    = '<uuid>'`` against the freshly-begun transaction — bracing the
    RLS policy whichever connection SQLAlchemy hands us this round.

    Called from the synchronous bridge that ``AsyncSession`` wraps;
    it runs before the path-operation function sees the session, and
    re-runs at the start of every subsequent transaction (after
    ``session.commit()`` triggers a new BEGIN).
    """
    tid = session.info.get("tenant_id")
    if tid is None:
        return
    # ``after_begin`` listeners receive the SQLAlchemy ``Connection``
    # for the just-started transaction. The synchronous Connection
    # offers ``execute(text(...))`` which we use directly.
    connection.execute(  # type: ignore[attr-defined]
        text(f"SET LOCAL app.current_tenant = '{tid}'")
    )


# Register once at import time. Targeting the ``Session`` class (the
# parent of every Session and the sync_session of every AsyncSession)
# means the listener fires for any session that stamps a tenant id
# into ``info`` — regardless of whether it was opened by the API,
# tests, or a future code path.
event.listen(Session, "after_begin", _set_current_tenant_on_begin)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield one ``AsyncSession`` per request with ``app.current_tenant`` set.

    Tenant resolution failure
        If ``resolve_tenant_id`` raises (no JWT claim and not in dev),
        the exception bubbles up through the dep chain as a 401 — the
        handler never runs. This is preferable to running with no
        tenant set, which under FORCE RLS would silently return zero
        rows (looking like "everything's fine").

    Tenant binding
        We stamp the tenant onto ``session.info`` and rely on the
        ``after_begin`` listener registered at module level to issue
        ``SET LOCAL app.current_tenant`` on every transaction. See
        the module docstring for why we don't just SET once at
        session open.
    """
    tenant_id: uuid.UUID = resolve_tenant_id(request)
    async with AsyncSessionLocal() as session:
        # ``session.info`` is the AsyncSession's user-data dict; it's
        # propagated to the underlying sync ``Session`` so the
        # ``after_begin`` listener can read it. Storing as a string
        # is fine — the listener interpolates verbatim.
        session.info["tenant_id"] = str(tenant_id)
        yield session
