"""Shared dependencies for the HTML / web routers under ``saebooks.routers``.

The two callables exported here:

* ``get_current_user`` — long-standing helper used by ``auth_router``
  and a handful of other routes; resolves the User row that
  ``ForwardAuthMiddleware`` stamped onto ``request.state`` after
  decoding the session JWT. Kept here so a single import path serves
  every router.

* ``get_web_session`` — request-scoped ``AsyncSession`` that binds
  ``app.current_tenant`` for every transaction the handler executes.
  Mirrors ``saebooks.api.v1.deps.get_session``: same
  ``session.info['tenant_id']`` stamp, same module-level
  ``after_begin`` listener installed on the synchronous ``Session``
  class — installed once by ``saebooks.api.v1.deps`` at import time.

Why a separate dep instead of reusing v1.deps.get_session
---------------------------------------------------------
The v1 dep is part of the JSON-API contract and depends on
``require_bearer`` (and the JSON 401-on-missing-tenant behaviour).
HTML routes don't necessarily run ``require_bearer`` — they're gated
by ``ForwardAuthMiddleware``, which decodes the session JWT and
stamps ``request.state.jwt_claims = {"tenant_id": str(user.tenant_id)}``
for the saebooks-web in-process client. Either way,
``resolve_tenant_id(request)`` already finds the tenant on every
authenticated HTML request.

What this dep gets us
---------------------
* Defence-in-depth: every transaction issues
  ``SET LOCAL app.current_tenant = '<uuid>'`` before any ORM query
  runs, so the ``tenant_isolation`` RLS policies installed by 0055
  and 0083 actively gate every read and write — even if a service
  forgets the explicit ``WHERE tenant_id = ...`` filter, RLS blocks
  the cross-tenant escape at the database.

* No duplication of the listener. ``saebooks.api.v1.deps`` registers
  the ``after_begin`` listener on the parent ``Session`` class at
  import time; that listener fires for every ``Session`` SQLAlchemy
  ever opens (including the one ``AsyncSession`` wraps). We just
  have to make sure the listener has been registered by the time the
  first web request lands. We do that with an explicit import of
  ``saebooks.api.v1.deps`` below — the import has the side effect of
  registering the listener.

Tenant resolution failure
-------------------------
If ``resolve_tenant_id`` raises (no JWT claims and not in dev),
the exception bubbles up from this dep as a 401 — consistent with
the v1 behaviour and with the rule "no tenant ⇒ no query". Without
this, a session-JWT misconfig would silently fall through to
"FORCE RLS returns zero rows" which looks like a benign empty
state in the UI rather than the auth failure it actually is.

Usage
-----
::

    from fastapi import Depends
    from sqlalchemy.ext.asyncio import AsyncSession
    from saebooks.routers.deps import get_web_session

    @router.get("/things")
    async def list_things(
        request: Request,
        session: AsyncSession = Depends(get_web_session),
    ) -> HTMLResponse:
        # every query under ``session`` has app.current_tenant set
        ...

Bypass / admin paths
--------------------
Routers that intentionally span tenants (admin / global queries) keep
using ``saebooks.services.tenant.bypass_tenant_scope`` plus a session
opened *without* a tenant id in ``info``. Don't add new bypasses
casually — every bypass is a potential cross-tenant leak.

Pre-auth pages (``/auth/login``, ``/healthz``, marketing redirects)
have no notion of "current tenant" and should not depend on this.
They can keep opening ``AsyncSessionLocal()`` directly when they
need DB access (e.g. user lookup during login).
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

# Importing the v1 deps module has the side effect of registering the
# ``after_begin`` listener on the synchronous Session class. Without
# this, ``get_web_session`` would stamp ``session.info['tenant_id']``
# but no one would issue ``SET LOCAL app.current_tenant`` — RLS would
# silently reject every query as "no tenant set".
from saebooks.api.v1 import deps as _v1_deps  # noqa: F401  (side-effect import)
from saebooks.api.v1.auth import resolve_tenant_id
from saebooks.db import AsyncSessionLocal
from saebooks.models.user import User
from saebooks.services import active_company as active_svc


async def get_current_user(request: Request) -> User:
    """Return the authenticated user, or raise 401.

    Reads ``request.state.user`` populated by
    ``saebooks.middleware.auth.ForwardAuthMiddleware`` after the
    session JWT was decoded.
    """
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return user


async def get_web_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield one ``AsyncSession`` per request, with ``app.current_tenant`` set.

    The session is used by every HTML router that touches customer
    data. Wiring is identical to v1's ``get_session`` — see this
    module's docstring for the rationale.

    Side-effect chain on each transaction:

    1. Caller (path operation) does ``await session.execute(...)`` or
       similar — SQLAlchemy starts a transaction.
    2. The ``after_begin`` listener installed by
       ``saebooks.api.v1.deps`` reads ``session.info['tenant_id']``
       and runs ``SET LOCAL app.current_tenant = '<uuid>'`` against
       the just-begun connection.
    3. Every subsequent statement on this transaction sees the GUC.
    4. Postgres RLS policies installed by 0055 + 0083 use
       ``current_setting('app.current_tenant', true)::uuid`` to gate
       reads + writes on every customer-data table.

    A ``session.commit()`` releases the connection (NullPool); the
    next query implicitly opens a new BEGIN, the listener fires
    again, and the GUC is re-applied. So commit-heavy services
    inside a single request are fine.
    """
    tenant_id = resolve_tenant_id(request)
    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = str(tenant_id)
        # Stamp company_id when ActiveCompanyMiddleware has already bound
        # the contextvar (web requests only). The after_begin listener then
        # issues SET LOCAL app.current_company_id on every transaction.
        active = active_svc.current_active_company()
        if active is not None:
            session.info["company_id"] = str(active.id)
        yield session
