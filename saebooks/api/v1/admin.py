"""JSON router — ``/api/v1/admin/audit-log`` + ``/api/v1/admin/sql/execute``.

Cat-C admin (Worker W5) — read-side audit trail + write-side SQL tool.

Two endpoints, both gated by admin role:

* ``GET /api/v1/admin/audit-log`` — paginated, filterable read of the
  ``change_log`` table. Filters: ``user_id``, ``route``, ``from_ts``,
  ``to_ts``, ``status``. The endpoint reads the same table that
  ``/api/v1/admin/sql/execute`` writes to (entity='sql_tool'), plus
  every domain-write that ``saebooks.services.change_log.append``
  drops. Tenant-scoped via the standard RLS plumbing.

* ``POST /api/v1/admin/sql/execute`` — gated by ``FLAG_SQL_TOOL`` (Pro+).
  Calls ``services.sql_tool.execute(...)`` which routes to the
  ``saebooks_sql_ro`` role for SELECT and to ``saebooks_app`` for
  admin-confirmed writes (see service docstring).

Why a separate v1 admin router instead of growing the legacy Jinja
``saebooks/routers/admin.py``? The Cat-C rollup is dropping
``saebooks/routers/admin.py`` once every screen is in the v1 path; the
SQL tool is the most security-critical surface to migrate first.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_session
from saebooks.api.v1.users import _require_admin
from saebooks.models.change_log import ChangeLog
from saebooks.services import audit as audit_svc
from saebooks.services import sql_tool as sql_svc
from saebooks.services.features import FLAG_AUDIT_SNAPSHOTS, FLAG_SQL_TOOL, require_feature
from saebooks.services.idempotency import (
    ClaimStatus,
    claim_or_fetch,
    store_response,
)

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_bearer), Depends(_require_admin)],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AuditLogEntry(BaseModel):
    """One row from ``change_log`` flattened for the audit-log API."""

    id: int
    entity: str
    entity_id: uuid.UUID
    op: str
    actor: str
    at: datetime
    version: int
    payload: dict[str, Any]


class AuditLogPage(BaseModel):
    items: list[AuditLogEntry]
    total: int
    limit: int
    offset: int


class WriteConfirmationBody(BaseModel):
    """Inline confirmation that an admin really meant to run a write."""

    enabled: bool = False
    verb_typed: str = ""


class SqlExecuteBody(BaseModel):
    statement: str = Field(min_length=1)
    write_confirmation: WriteConfirmationBody | None = None


class SqlExecuteResponse(BaseModel):
    rows: list[list[Any]]
    columns: list[str]
    rowcount: int
    role_used: str
    audit_id: int
    truncated: bool


class AuditSnapshotEntry(BaseModel):
    """One row from ``audit_snapshots`` — the FLAG_AUDIT_SNAPSHOTS browse API."""

    id: uuid.UUID
    table_name: str
    row_id: str
    action: str
    before_data: dict[str, Any]
    after_data: dict[str, Any] | None
    reason: str | None
    performed_by: str | None
    created_at: datetime


class AuditSnapshotPage(BaseModel):
    items: list[AuditSnapshotEntry]
    total: int
    limit: int
    offset: int


class AuditSnapshotFilterOptions(BaseModel):
    tables: list[str]
    actors: list[str]


# ---------------------------------------------------------------------------
# /admin/audit-log — paginated list of change_log rows
# ---------------------------------------------------------------------------


def _parse_iso_ts(raw: str | None, *, name: str) -> datetime | None:
    """Accept ISO-8601 date or datetime; raise 400 on garbage."""
    if raw is None or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(
            400, f"{name} must be ISO-8601 (date or datetime), got {raw!r}"
        ) from exc


@router.get(
    "/audit-log",
    response_model=AuditLogPage,
    summary="Paginated, filterable audit log",
)
async def get_audit_log(
    request: Request,
    user_id: str | None = Query(default=None, description="Filter to this actor user UUID"),
    route: str | None = Query(default=None, description="Filter to this entity / route slug"),
    from_ts: str | None = Query(default=None, description="ISO-8601 lower bound on at"),
    to_ts: str | None = Query(default=None, description="ISO-8601 upper bound on at"),
    status: str | None = Query(default=None, description="Filter on payload.status (sql_tool rows only)"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> AuditLogPage:
    """List ``change_log`` rows with filters.

    Tenant scoping: ``change_log`` carries a ``tenant_id`` column (added
    by migration 0118) and has FORCE RLS with a ``tenant_isolation``
    policy. Every row is filtered to the caller's tenant via
    ``ChangeLog.tenant_id == tenant_id`` — no special-casing for
    entity type. (Lane 5 P0-007: the previous filter let every
    non-sql_tool row through regardless of tenant.)
    """
    from_dt = _parse_iso_ts(from_ts, name="from_ts")
    to_dt = _parse_iso_ts(to_ts, name="to_ts")
    tenant_id = resolve_tenant_id(request)

    filters = []
    if user_id is not None and user_id.strip():
        # Match either the explicit payload.user_id (sql_tool rows) or
        # the legacy actor='user:<uuid>' format (domain rows). Use a
        # JSONB existence test — cheap on a partial index, even cheaper
        # without one given typical change_log volume.
        filters.append(
            ChangeLog.payload["user_id"].astext == user_id.strip()
        )
    if route is not None and route.strip():
        filters.append(ChangeLog.entity == route.strip())
    if from_dt is not None:
        filters.append(ChangeLog.at >= from_dt)
    if to_dt is not None:
        filters.append(ChangeLog.at <= to_dt)
    if status is not None and status.strip():
        filters.append(ChangeLog.payload["status"].astext == status.strip())

    # Tenant-scope: filter every row to the caller's tenant.
    # change_log.tenant_id was added by migration 0118 (P0-007 fix).
    # RLS enforces this at the DB level too; this is defence-in-depth.
    filters.append(ChangeLog.tenant_id == tenant_id)

    where = and_(*filters) if filters else None

    count_stmt = select(func.count()).select_from(ChangeLog)
    if where is not None:
        count_stmt = count_stmt.where(where)
    total = (await session.execute(count_stmt)).scalar_one()

    list_stmt = select(ChangeLog).order_by(ChangeLog.id.desc())
    if where is not None:
        list_stmt = list_stmt.where(where)
    list_stmt = list_stmt.limit(limit).offset(offset)
    rows = (await session.execute(list_stmt)).scalars().all()

    return AuditLogPage(
        items=[
            AuditLogEntry(
                id=r.id,
                entity=r.entity,
                entity_id=r.entity_id,
                op=r.op,
                actor=r.actor,
                at=r.at,
                version=r.version,
                payload=r.payload or {},
            )
            for r in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# /admin/sql/execute — gated by FLAG_SQL_TOOL
# ---------------------------------------------------------------------------


@router.post(
    "/sql/execute",
    response_model=SqlExecuteResponse,
    summary="Execute one ad-hoc SQL statement and append an audit row",
    dependencies=[Depends(require_feature(FLAG_SQL_TOOL))],
)
async def post_sql_execute(
    body: SqlExecuteBody,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Run a single SQL statement on the admin SQL tool.

    The bulk of the work — RO/RW connection routing, RLS binding,
    audit-row writing — lives in ``services.sql_tool.execute``.  This
    endpoint just wires up auth, the FLAG gate, idempotency, and the
    JSON envelope.

    Idempotency: an ``X-Idempotency-Key`` header replays the cached
    response on repeat. The cached body is the full
    ``SqlExecuteResponse`` from the original run — including the
    original ``audit_id``, so the audit log shows one row per logical
    request even if the client retries on a transient network error.
    """
    tenant_id = resolve_tenant_id(request)
    user = getattr(request.state, "user", None)
    user_id: uuid.UUID | None = getattr(user, "id", None) if user is not None else None

    key = idempotency_key.strip() if idempotency_key else None
    if key:
        raw = await request.body()
        body_sha = hashlib.sha256(raw).hexdigest()
        claim = await claim_or_fetch(session, key, tenant_id, body_sha)
        if claim.status == ClaimStatus.CONFLICT:
            return JSONResponse(
                {
                    "code": "idempotency_key_conflict",
                    "message": "X-Idempotency-Key reused with a different request body",
                },
                status_code=422,
            )
        if claim.status == ClaimStatus.IN_FLIGHT:
            return JSONResponse(
                {
                    "code": "request_in_flight",
                    "message": "Request with this idempotency key is currently being processed.",
                },
                status_code=503,
                headers={"Retry-After": "1"},
            )
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 200,
            )
        # CLAIMED — fall through.

    confirm = None
    if body.write_confirmation is not None:
        confirm = sql_svc.WriteConfirmation(
            enabled=body.write_confirmation.enabled,
            verb_typed=body.write_confirmation.verb_typed,
        )

    try:
        result = await sql_svc.execute(
            session,
            statement=body.statement,
            write_confirmation=confirm,
            user_id=user_id,
            tenant_id=tenant_id,
        )
    except sql_svc.WriteRejectedError as exc:
        # 403 — admin tried to write without a matching confirmation.
        # Audit row already written by the service.
        body_obj = {
            "code": "write_rejected",
            "message": str(exc),
            "audit_id": exc.audit_id,
        }
        if key:
            await store_response(session, key, 403, json.dumps(body_obj).encode())
            await session.commit()
        return JSONResponse(body_obj, status_code=403)
    except sql_svc.QueryError as exc:
        body_obj = {
            "code": "query_error",
            "message": str(exc),
        }
        if key:
            await store_response(session, key, 400, json.dumps(body_obj).encode())
            await session.commit()
        return JSONResponse(body_obj, status_code=400)

    response = SqlExecuteResponse(
        rows=result.rows,
        columns=result.columns,
        rowcount=result.rowcount,
        role_used=result.role_used,
        audit_id=result.audit_id,
        truncated=result.truncated,
    )
    response_body = response.model_dump(mode="json")
    if key:
        await store_response(session, key, 200, json.dumps(response_body).encode())
        await session.commit()
    return JSONResponse(response_body, status_code=200)


# ---------------------------------------------------------------------------
# /admin/audit-snapshots — Wave C, gated by FLAG_AUDIT_SNAPSHOTS (Pro+)
# ---------------------------------------------------------------------------
# Capture itself (services/audit.py's snapshot()/snapshot_row(), called from
# 7 services) is ALWAYS ON at every edition — it's the point-in-time
# undo/recovery mechanism CHARTER §7.3 requires unconditionally. What's
# gated here is the browse/point-in-time VIEW into that data (CHARTER
# §12.1 "Audit snapshot service" row, Pro+) — per Richard's decision 8
# ("keep existing row-level before/after as the point-in-time MVP + add
# the tenant_id+FORCE-RLS+isolation-policy migration + backfill before
# exposing the browse API, then gate"). Migration 0186 landed the RLS
# remediation; these three routes are the first thing that could ever
# read the table directly (previously nothing did — see that migration's
# docstring for why 0055 originally left audit_snapshots unscoped).
#
# Tenant scoping is defence-in-depth: FORCE RLS (0186) is the real
# boundary; the explicit ``tenant_id=`` filter on every call below
# mirrors ``get_audit_log``'s belt-and-braces pattern above.


@router.get(
    "/audit-snapshots/_filter_options",
    response_model=AuditSnapshotFilterOptions,
    summary="Distinct table_name / performed_by values for the filter dropdowns",
    dependencies=[Depends(require_feature(FLAG_AUDIT_SNAPSHOTS))],
)
async def get_audit_snapshot_filter_options(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> AuditSnapshotFilterOptions:
    tenant_id = resolve_tenant_id(request)
    tables = await audit_svc.distinct_tables(session, tenant_id=tenant_id)
    actors = await audit_svc.distinct_actors(session, tenant_id=tenant_id)
    return AuditSnapshotFilterOptions(tables=tables, actors=actors)


@router.get(
    "/audit-snapshots",
    response_model=AuditSnapshotPage,
    summary="Paginated, filterable browse of audit_snapshots (point-in-time recovery data)",
    dependencies=[Depends(require_feature(FLAG_AUDIT_SNAPSHOTS))],
)
async def get_audit_snapshots(
    request: Request,
    table_name: str | None = Query(default=None),
    row_id: str | None = Query(default=None),
    action: str | None = Query(default=None),
    performed_by: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> AuditSnapshotPage:
    tenant_id = resolve_tenant_id(request)
    filters: dict[str, Any] = {
        "table_name": table_name,
        "row_id": row_id,
        "action": action,
        "performed_by": performed_by,
        "tenant_id": tenant_id,
    }
    total = await audit_svc.count_browse(session, **filters)
    rows = await audit_svc.browse(session, limit=limit, offset=offset, **filters)
    return AuditSnapshotPage(
        items=[
            AuditSnapshotEntry(
                id=r.id,
                table_name=r.table_name,
                row_id=r.row_id,
                action=r.action,
                before_data=r.before_data,
                after_data=r.after_data,
                reason=r.reason,
                performed_by=r.performed_by,
                created_at=r.created_at,
            )
            for r in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/audit-snapshots/{snapshot_id}",
    response_model=AuditSnapshotEntry,
    summary="Fetch one audit_snapshots row by id",
    dependencies=[Depends(require_feature(FLAG_AUDIT_SNAPSHOTS))],
)
async def get_audit_snapshot(
    snapshot_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> AuditSnapshotEntry:
    tenant_id = resolve_tenant_id(request)
    snap = await audit_svc.get_snapshot(session, snapshot_id, tenant_id=tenant_id)
    if snap is None:
        # 404 for both "doesn't exist" and "exists but belongs to another
        # tenant" — same not-403 posture as every other cross-tenant probe
        # in this codebase (journal.get, etc.): don't confirm the row
        # exists to a caller who can't see it.
        raise HTTPException(404, "Audit snapshot not found")
    return AuditSnapshotEntry(
        id=snap.id,
        table_name=snap.table_name,
        row_id=snap.row_id,
        action=snap.action,
        before_data=snap.before_data,
        after_data=snap.after_data,
        reason=snap.reason,
        performed_by=snap.performed_by,
        created_at=snap.created_at,
    )
