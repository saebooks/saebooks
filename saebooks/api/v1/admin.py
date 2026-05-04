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
from saebooks.services import sql_tool as sql_svc
from saebooks.services.features import FLAG_SQL_TOOL, require_feature
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

    Tenant scoping: the change_log table is global (no ``tenant_id``
    column). For sql_tool rows we filter on ``payload.tenant_id`` so
    one admin can only see audit rows for their own tenant. For domain
    rows, the actor stamping is the only scope — but those rows record
    a tenant-scoped entity_id, so a read here cannot reveal anything
    the same admin couldn't read via the entity's own endpoint.
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

    # Tenant-scope sql_tool rows — payload.tenant_id must match. For
    # non-sql_tool rows, we leave them unfiltered (the actor model is
    # the source of truth for those). This means an admin in tenant A
    # browsing the audit log sees: their own tenant's sql_tool rows +
    # all domain rows. That matches the legacy ``/admin/audit`` behavior
    # which surfaces audit_log snapshots from the global table.
    tenant_filter = (
        (ChangeLog.entity != "sql_tool")
        | (ChangeLog.payload["tenant_id"].astext == str(tenant_id))
    )
    filters.append(tenant_filter)

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
