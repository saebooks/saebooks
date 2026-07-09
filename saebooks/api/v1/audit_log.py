"""JSON router — ``GET /api/v1/audit-log`` (real audit_log reader).

C2 PR5. Surfaces the attributable hot-path audit trail written by
``services.audit_log.append`` into the ``audit_log`` table (invoice/bill/
payment/credit-note post & void, JE override-post, hard-delete forensics).

Route-collision note
--------------------
The pre-existing ``GET /api/v1/admin/audit-log`` (in ``admin.py``, router
prefix ``/admin``) is MISLABELLED — it reads the ``change_log`` table, not
``audit_log``. Rather than repoint it (which would break its contract tests
in ``tests/api/v1/test_admin.py`` and ``tests/test_audit_export.py``), this
new reader takes the distinct ``/api/v1/audit-log`` path (no ``/admin``
segment). The two endpoints therefore do not collide:

  * ``/api/v1/admin/audit-log`` -> change_log (legacy, unchanged)
  * ``/api/v1/audit-log``       -> audit_log  (this reader, the real one)

Admin-gated and tenant-scoped, mirroring the admin router.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_session
from saebooks.api.v1.users import _require_admin
from saebooks.models.audit_log import AuditLog

router = APIRouter(
    prefix="/audit-log",
    tags=["audit-log"],
    dependencies=[Depends(require_bearer), Depends(_require_admin)],
)


class AuditLogEntry(BaseModel):
    id: uuid.UUID
    actor_user_id: uuid.UUID
    action: str
    table_name: str
    row_id: str
    row_snapshot: dict[str, Any]
    reason: str | None
    at: datetime


class AuditLogPage(BaseModel):
    items: list[AuditLogEntry]
    total: int
    limit: int
    offset: int


@router.get(
    "",
    response_model=AuditLogPage,
    summary="Read the real audit_log table (admin-only, tenant-scoped)",
)
async def get_audit_log(
    request: Request,
    row_id: str | None = Query(default=None, description="Filter to this affected row id"),
    table: str | None = Query(default=None, description="Filter to this table_name"),
    action: str | None = Query(default=None, description="Filter to this action value"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> AuditLogPage:
    """List ``audit_log`` rows newest-first, filtered by ``row_id`` / ``table``.

    Tenant scoping is enforced both by the FORCE-RLS ``tenant_isolation``
    policy on ``audit_log`` and, defence-in-depth, by an explicit
    ``tenant_id`` predicate here.
    """
    tenant_id = resolve_tenant_id(request)
    filters = [AuditLog.tenant_id == tenant_id]
    if row_id is not None and row_id.strip():
        filters.append(AuditLog.row_id == row_id.strip())
    if table is not None and table.strip():
        filters.append(AuditLog.table_name == table.strip())
    if action is not None and action.strip():
        filters.append(AuditLog.action == action.strip())
    where = and_(*filters)

    total = (
        await session.execute(select(func.count()).select_from(AuditLog).where(where))
    ).scalar_one()

    rows = (
        await session.execute(
            select(AuditLog)
            .where(where)
            .order_by(AuditLog.at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()

    return AuditLogPage(
        items=[
            AuditLogEntry(
                id=r.id,
                actor_user_id=r.actor_user_id,
                action=r.action,
                table_name=r.table_name,
                row_id=r.row_id,
                row_snapshot=r.row_snapshot or {},
                reason=r.reason,
                at=r.at,
            )
            for r in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )
