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

import csv
import io
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_session
from saebooks.api.v1.users import _require_admin
from saebooks.models.audit_log import AuditLog

# CSV formula-injection guard — a cell whose first character is one of these
# is a live formula in Excel/Sheets/LibreOffice when the file is opened.
# Prefixing a single quote forces it to render as text instead of
# executing. Applied ONLY to free-text columns (reason/action/table_name/
# row_id) — never to `at` (datetime) or `actor_user_id` (UUID), which can't
# carry attacker-controlled content. "\t"/"\r" are included too — a leading
# tab or carriage return is also treated as a formula lead-in by some
# spreadsheet apps' CSV importers.
_FORMULA_LEAD_CHARS = ("=", "+", "-", "@", "\t", "\r")

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


def _require_tz_aware(at_from: datetime | None, at_to: datetime | None) -> None:
    """Reject naive ``at_from``/``at_to`` values.

    A naive datetime compared against the ``timestamptz`` ``at`` column is
    silently interpreted in the server's session timezone, not the
    caller's — on this deployment that's a ~10h Brisbane offset error with
    no error raised. Better to 422 loudly than filter wrong.
    """
    for name, value in (("at_from", at_from), ("at_to", at_to)):
        if value is not None and value.tzinfo is None:
            raise HTTPException(
                422,
                f"{name} must be timezone-aware ISO-8601, e.g. "
                "2026-07-01T00:00:00+10:00",
            )


def _apply_filters(
    tenant_id: uuid.UUID,
    *,
    row_id: str | None,
    table: str | None,
    action: str | None,
    at_from: datetime | None,
    at_to: datetime | None,
    actor_user_id: uuid.UUID | None,
) -> Any:
    filters = [AuditLog.tenant_id == tenant_id]
    if row_id is not None and row_id.strip():
        filters.append(AuditLog.row_id == row_id.strip())
    if table is not None and table.strip():
        filters.append(AuditLog.table_name == table.strip())
    if action is not None and action.strip():
        filters.append(AuditLog.action == action.strip())
    if at_from is not None:
        filters.append(AuditLog.at >= at_from)
    if at_to is not None:
        filters.append(AuditLog.at <= at_to)
    if actor_user_id is not None:
        filters.append(AuditLog.actor_user_id == actor_user_id)
    return and_(*filters)


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
    at_from: datetime | None = Query(default=None, description="Filter to at >= this instant"),
    at_to: datetime | None = Query(default=None, description="Filter to at <= this instant"),
    actor_user_id: uuid.UUID | None = Query(default=None, description="Filter to this actor"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> AuditLogPage:
    """List ``audit_log`` rows newest-first, filtered by ``row_id`` / ``table``.

    Also filterable by an ``at`` date-range (``at_from``/``at_to``,
    inclusive both ends) and by ``actor_user_id``.

    Tenant scoping is enforced both by the FORCE-RLS ``tenant_isolation``
    policy on ``audit_log`` and, defence-in-depth, by an explicit
    ``tenant_id`` predicate here.
    """
    _require_tz_aware(at_from, at_to)
    tenant_id = resolve_tenant_id(request)
    where = _apply_filters(
        tenant_id,
        row_id=row_id,
        table=table,
        action=action,
        at_from=at_from,
        at_to=at_to,
        actor_user_id=actor_user_id,
    )

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


_CSV_ROW_CAP = 10_000
_CSV_COLUMNS = ["at", "actor_user_id", "action", "table_name", "row_id", "reason"]


def _csv_guard(value: str) -> str:
    """Prefix a leading formula-lead character with a single quote.

    Blocks CSV formula injection (an operator opening the export in
    Excel/Sheets and having a crafted ``reason``/``row_id`` execute as a
    formula). Only applied to free-text columns — never to ``at`` or
    ``actor_user_id``.
    """
    if value and value[0] in _FORMULA_LEAD_CHARS:
        return f"'{value}"
    return value


@router.get(
    ".csv",
    summary="CSV export of the real audit_log table (admin-only, tenant-scoped)",
)
async def get_audit_log_csv(
    request: Request,
    row_id: str | None = Query(default=None, description="Filter to this affected row id"),
    table: str | None = Query(default=None, description="Filter to this table_name"),
    action: str | None = Query(default=None, description="Filter to this action value"),
    at_from: datetime | None = Query(default=None, description="Filter to at >= this instant"),
    at_to: datetime | None = Query(default=None, description="Filter to at <= this instant"),
    actor_user_id: uuid.UUID | None = Query(default=None, description="Filter to this actor"),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """CSV export of ``audit_log`` rows, same filters as the JSON list route.

    No ``limit``/``offset`` — instead a hard cap of ``_CSV_ROW_CAP``
    (10,000) rows, newest-first (``ORDER BY at DESC``), so an export can
    never run away on an unbounded query. Columns are
    ``at,actor_user_id,action,table_name,row_id,reason`` — ``row_snapshot``
    is deliberately excluded: it's a JSON blob and doesn't belong in a CSV
    cell. Free-text cells are guarded against CSV formula injection (see
    ``_csv_guard``).

    Two response headers surface the truncation, since the body shape
    itself (a bare CSV) has nowhere to carry it: ``X-Total-Count`` is the
    real count of matching rows (same ``func.count()`` query the JSON
    route uses) regardless of whether it exceeds the cap, and
    ``X-Truncated: true`` is set additionally when the export is capped
    short of that total.
    """
    _require_tz_aware(at_from, at_to)
    tenant_id = resolve_tenant_id(request)
    where = _apply_filters(
        tenant_id,
        row_id=row_id,
        table=table,
        action=action,
        at_from=at_from,
        at_to=at_to,
        actor_user_id=actor_user_id,
    )

    total = (
        await session.execute(select(func.count()).select_from(AuditLog).where(where))
    ).scalar_one()

    rows = (
        await session.execute(
            select(AuditLog)
            .where(where)
            .order_by(AuditLog.at.desc())
            .limit(_CSV_ROW_CAP)
        )
    ).scalars().all()

    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(_CSV_COLUMNS)
    for r in rows:
        writer.writerow([
            r.at.isoformat(),
            str(r.actor_user_id),
            _csv_guard(r.action),
            _csv_guard(r.table_name),
            _csv_guard(r.row_id),
            _csv_guard(r.reason) if r.reason is not None else "",
        ])

    headers = {
        "Content-Disposition": 'attachment; filename="audit_log.csv"',
        "X-Total-Count": str(total),
    }
    if total > _CSV_ROW_CAP:
        headers["X-Truncated"] = "true"

    return Response(
        content=buf.getvalue().encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers=headers,
    )
