"""Active-company resolution for web routers.

The web UI lets a user be in one company at a time. We persist the
choice in a signed-free cookie (``active_company_id``) — same surface
area as the API's ``X-Company-Id`` header, just bound to the browser.

Resolution order
----------------
1. Cookie value, if present and the UUID belongs to the request's
   tenant and the company isn't archived.
2. First non-archived company in the request's tenant, ordered by
   ``created_at``. Same fallback the legacy ``_first_company`` helper
   in every router used; this preserves single-company behaviour for
   installs that never explicitly switch.
3. ``HTTPException(500)`` if the tenant has zero active companies —
   that's a deployment bug, not a runtime condition the user should
   handle.

The cookie is **not** signed. Forging it can only point the browser
at *another company in the same tenant* — RLS already blocks
cross-tenant reads, and ``assert_company_owned`` blocks cross-company
FK references on writes. A user inside the tenant who can already
log in can already see every company they own; the cookie just picks
which one the UI shows. So a signed cookie would be belt-on-belt
without changing the threat model.

This module deliberately does **not** bind the contextvar in
``services/tenant.py``. The contextvar is for ORM-listener defence in
depth and would still be set by a dedicated middleware (P1) when we
add row-level company scoping. Today the explicit ``company_id``
filters in every service do the work; the cookie just selects which
id to filter by.
"""
from __future__ import annotations

import uuid
from typing import Sequence

from fastapi import HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import resolve_tenant_id
from saebooks.models.company import Company

COOKIE_NAME = "active_company_id"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # one year — purely a UX preference


def _read_cookie_uuid(request: Request) -> uuid.UUID | None:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError):
        return None


async def list_companies(
    session: AsyncSession, tenant_id: uuid.UUID
) -> list[Company]:
    """Return every non-archived company in ``tenant_id``, name-ordered."""
    result = await session.execute(
        select(Company)
        .where(
            Company.tenant_id == tenant_id,
            Company.archived_at.is_(None),
        )
        .order_by(Company.name)
    )
    return list(result.scalars().all())


async def resolve_active_company(
    session: AsyncSession,
    request: Request,
    tenant_id: uuid.UUID | None = None,
) -> Company:
    """Return the company the current request should act in.

    See module docstring for the resolution order. ``tenant_id`` is
    resolved from the request when omitted so callers don't have to
    chain it through.
    """
    if tenant_id is None:
        tenant_id = resolve_tenant_id(request)

    cookie_id = _read_cookie_uuid(request)
    if cookie_id is not None:
        result = await session.execute(
            select(Company).where(
                Company.id == cookie_id,
                Company.tenant_id == tenant_id,
                Company.archived_at.is_(None),
            )
        )
        company = result.scalars().first()
        if company is not None:
            return company

    # Fallback: first by created_at — same legacy behaviour as
    # ``_first_company`` so single-company installs keep working
    # before they ever set the cookie.
    result = await session.execute(
        select(Company)
        .where(
            Company.tenant_id == tenant_id,
            Company.archived_at.is_(None),
        )
        .order_by(Company.created_at)
    )
    company = result.scalars().first()
    if company is None:
        raise HTTPException(500, "No active company")
    return company


async def resolve_active_with_options(
    session: AsyncSession,
    request: Request,
    tenant_id: uuid.UUID | None = None,
) -> tuple[Company, list[Company]]:
    """Convenience: active company + full list, one tenant fetch.

    Used by routers that render templates including the company
    switcher — the switcher needs the full list and would otherwise
    re-query.
    """
    if tenant_id is None:
        tenant_id = resolve_tenant_id(request)
    companies = await list_companies(session, tenant_id)
    cookie_id = _read_cookie_uuid(request)
    active: Company | None = None
    if cookie_id is not None:
        active = next((c for c in companies if c.id == cookie_id), None)
    if active is None:
        # First by created_at; ``list_companies`` returns name-ordered
        # so we re-pick by created_at explicitly here.
        active = min(companies, key=lambda c: c.created_at) if companies else None
    if active is None:
        raise HTTPException(500, "No active company")
    return active, companies


def set_active_cookie(response: Response, company_id: uuid.UUID) -> None:
    """Write the active-company cookie on ``response``.

    HttpOnly is off — there's no auth value in the cookie, just a
    UUID, and the JS UI may want to read it for client-side hints.
    SameSite=Lax is plenty for a browser-only switcher.
    """
    response.set_cookie(
        COOKIE_NAME,
        str(company_id),
        max_age=COOKIE_MAX_AGE,
        httponly=False,
        samesite="lax",
        path="/",
    )


def first_by_created(companies: Sequence[Company]) -> Company | None:
    """Helper for templates / tests."""
    if not companies:
        return None
    return min(companies, key=lambda c: c.created_at)
