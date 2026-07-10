"""Auth + tenant-context dependencies for the pre-accounting module app.

Two concerns, both mirrored from existing engine machinery:

1. **Inbound auth** — every ``/module/preaccounting/*`` route is gated by a
   shared secret presented as ``X-PreAccounting-Token`` and compared in
   constant time against ``settings.preaccounting_token`` (env
   ``PREACCOUNTING_TOKEN``). An EMPTY configured token HARD-DISABLES the
   surface with 503 in every environment: this module fronts tenant data,
   so an unconfigured instance must fail closed rather than serve ungated.
   This is the same fail-closed contract as ``api/internal/auth.py``.

2. **Tenant context** — the module has no JWT. The delegating engine sends
   the tenant (and, when known, the active company) as ``X-Tenant-Id`` /
   ``X-Company-Id`` headers. We stamp them onto ``session.info`` exactly as
   ``api/v1/deps.get_session`` does for the JWT path, and rely on the
   process-wide ``after_begin`` listener registered in ``api/v1/deps`` to
   re-issue ``SET LOCAL app.current_tenant`` on every transaction. Importing
   ``saebooks.api.v1.deps`` below guarantees that listener is installed.
"""
from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

# Side-effect import: registers the process-wide ``after_begin`` listener that
# turns ``session.info['tenant_id']`` into ``SET LOCAL app.current_tenant`` and
# the ``before_flush`` tenant-backfill. Without this the module would open
# sessions whose RLS predicate is never bound.
from saebooks.api.v1 import deps as _v1_deps  # noqa: F401
from saebooks.config import settings
from saebooks.db import AsyncSessionLocal


async def require_preaccounting_token(
    x_preaccounting_token: str | None = Header(
        default=None, alias="X-PreAccounting-Token"
    ),
) -> None:
    """Gate a module route on the ``X-PreAccounting-Token`` shared secret.

    503 when no token is configured (fail-closed — the module fronts tenant
    data); 401 when the presented header is missing or does not match.
    """
    expected = settings.preaccounting_token.strip()
    if not expected:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "pre-accounting module disabled: PREACCOUNTING_TOKEN is not configured",
        )
    presented = (x_preaccounting_token or "").strip()
    if not presented or not secrets.compare_digest(presented, expected):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid or missing X-PreAccounting-Token",
        )


def _parse_tenant_id(request: Request) -> uuid.UUID:
    raw = request.headers.get("X-Tenant-Id")
    if not raw:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "X-Tenant-Id header is required (module has no JWT to derive it)",
        )
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "X-Tenant-Id must be a UUID"
        ) from exc


def _parse_company_id(request: Request) -> uuid.UUID | None:
    raw = request.headers.get("X-Company-Id")
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "X-Company-Id must be a UUID"
        ) from exc


class TenantContext:
    """Resolved (tenant_id, company_id) for a module request."""

    __slots__ = ("company_id", "tenant_id")

    def __init__(self, tenant_id: uuid.UUID, company_id: uuid.UUID | None) -> None:
        self.tenant_id = tenant_id
        self.company_id = company_id


async def get_tenant_context(request: Request) -> TenantContext:
    """Resolve the tenant/company context from the module's headers."""
    return TenantContext(_parse_tenant_id(request), _parse_company_id(request))


async def get_module_session(
    ctx: TenantContext = Depends(get_tenant_context),
) -> AsyncIterator[AsyncSession]:
    """Yield an RLS-bound ``AsyncSession`` for the tenant in the headers.

    Stamps ``session.info`` so the ``after_begin`` listener installed by
    ``api/v1/deps`` issues ``SET LOCAL app.current_tenant`` (and
    ``app.current_company_id`` when a company is supplied) on every
    transaction — including the internal commits the service layer makes
    (e.g. the DRAFT-invoice fact created during quote→invoice conversion).
    """
    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = str(ctx.tenant_id)
        if ctx.company_id is not None:
            session.info["company_id"] = str(ctx.company_id)
        yield session
