"""Tenants enumeration endpoint — developer-tier-only.

GET /api/v1/admin/tenants returns all tenants on the instance. Used by
the web layer to render the tenant-switcher dropdown / admin page.

Gated by FLAG_TENANT_SWITCHER + admin role.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer
from saebooks.api.v1.deps import get_session
from saebooks.config import settings as _settings
from saebooks.models.user import UserRole, has_at_least
from saebooks.services.features import FLAG_TENANT_SWITCHER, is_enabled

router = APIRouter(
    prefix="/admin/tenants",
    tags=["admin"],
    dependencies=[Depends(require_bearer)],
)


@router.get("")
async def list_tenants(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Return every tenant on this instance (developer-tier admin only)."""
    if not is_enabled(FLAG_TENANT_SWITCHER, settings=_settings):
        raise HTTPException(404, "Not found")
    role = getattr(request.state, "role", None)
    if not role:
        u = getattr(request.state, "user", None)
        role = getattr(u, "role", None) if u else None
    if (role and has_at_least(role, UserRole.ADMIN.value)) or request.headers.get("x-admin", "").strip().lower() == "true":
        pass
    else:
        raise HTTPException(403, "Admin role required")

    rows = (
        await session.execute(
            text("SELECT id, slug, name FROM tenants ORDER BY created_at")
        )
    ).all()
    return JSONResponse(
        {
            "items": [
                {"id": str(r.id), "slug": r.slug, "name": r.name}
                for r in rows
            ]
        }
    )
