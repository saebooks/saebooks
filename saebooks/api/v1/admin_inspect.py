"""Raw entity inspector — developer-tier-only debug endpoint.

GET /api/v1/admin/inspect/{entity_table}/{row_id} returns the raw row JSON
plus the most recent 10 change_log entries for that entity. Used by the
"View raw" button on every detail page when FLAG_RAW_JSON_INSPECTOR is
active.

Gated by:
  1. FLAG_RAW_JSON_INSPECTOR active in the current edition (developer only).
  2. Caller is admin (request.state.role).

Returns 403 when the flag is off or the role is not admin. 404 when the
row doesn't exist (RLS-scoped — foreign-tenant rows look gone).

Why a generic table-name endpoint rather than per-entity? The inspector
is a debug affordance — operational caller already knows which table they
want to look at. A per-entity endpoint would mean 22 thin handlers and
all the same security boilerplate. Single endpoint keeps it tight.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_session
from saebooks.config import settings as _settings
from saebooks.models.user import UserRole, has_at_least
from saebooks.services.features import FLAG_RAW_JSON_INSPECTOR, is_enabled

router = APIRouter(
    prefix="/admin/inspect",
    tags=["admin"],
    dependencies=[Depends(require_bearer)],
)


# Allowed tables — anything we don't whitelist returns 404. Prevents
# arbitrary SELECTs against tables the operator wasn't supposed to peek at.
_ALLOWED_TABLES: frozenset[str] = frozenset({
    "accounts", "account_ranges", "allocation_rules",
    "bank_feed_accounts", "bank_rules", "bank_statement_lines", "bills",
    "budgets", "change_log",
    "companies", "contacts", "credit_notes",
    "employees", "expenses",
    "fixed_assets",
    "invoices", "items",
    "journal_entries", "journal_lines", "journal_templates",
    "payments", "projects", "purchase_orders",
    "quotes", "quote_lines",
    "recurring_invoices",
    "super_funds",
    "tax_codes", "tax_periods", "tax_returns",
    "time_entries",
})


def _ensure_dev_admin(request: Request) -> None:
    if not is_enabled(FLAG_RAW_JSON_INSPECTOR, settings=_settings):
        raise HTTPException(404, "Not found")
    role: str | None = getattr(request.state, "role", None)
    user = getattr(request.state, "user", None)
    if not role and user is not None:
        role = getattr(user, "role", None)
    if role and has_at_least(role, UserRole.ADMIN.value):
        return
    # Dev-token path doesn't populate request.state.user — fall back to
    # X-Admin: true (same pattern as hard_delete_admin_gate).
    if request.headers.get("x-admin", "").strip().lower() == "true":
        return
    raise HTTPException(403, "Admin role required")


@router.get("/{table}/{row_id}")
async def inspect_row(
    table: str,
    row_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Return the row + its recent change_log entries as raw JSON."""
    _ensure_dev_admin(request)
    if table not in _ALLOWED_TABLES:
        raise HTTPException(404, f"Table {table!r} is not inspectable")

    tenant_id = resolve_tenant_id(request)

    # ROW — to_jsonb gives us a clean dict without ORM round-tripping.
    row_q = text(f"SELECT to_jsonb(t.*) AS j FROM {table} t WHERE id = :rid")
    row_row = (await session.execute(row_q, {"rid": row_id})).first()
    if row_row is None:
        raise HTTPException(404, f"{table}/{row_id} not found")
    row_json: dict[str, Any] = row_row.j

    # change_log — last 10 entries for this entity. RLS scoped to current
    # tenant; setting the tenant explicitly so the inspector doesn't leak
    # cross-tenant change_log rows when the operator forgot to set it
    # earlier in the request.
    await session.execute(
        text("SELECT set_config('app.current_tenant', :t, true)"),
        {"t": str(tenant_id)},
    )
    cl_q = text(
        "SELECT id, op, actor, version, at, payload "
        "FROM change_log "
        "WHERE entity_id = :rid "
        "ORDER BY id DESC LIMIT 10"
    )
    cl_rows = (await session.execute(cl_q, {"rid": row_id})).all()
    change_log = [
        {
            "id": r.id,
            "op": r.op,
            "actor": r.actor,
            "version": r.version,
            "at": r.at.isoformat() if r.at else None,
            "payload": r.payload,
        }
        for r in cl_rows
    ]

    return JSONResponse(
        {
            "table": table,
            "id": str(row_id),
            "row": row_json,
            "change_log": change_log,
            "change_log_count": len(change_log),
        }
    )
