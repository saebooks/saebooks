"""JSON router — ``/api/v1/email-log``.

Read-only view onto the email_send_log audit table. Rows are written by
``saebooks.services.customer_email.send_customer_email`` — this module just
serves them back so the UI can show send history.

Tenant isolation is enforced by the existing ``tenant_isolation`` RLS policy
on email_send_log (see migration 0123). We additionally pass the tenant
filter explicitly so the planner can use the (tenant_id, sent_at) index.

Endpoints:
    GET /email-log              — paginated list, filterable
    GET /email-log/{id}         — single row detail
    GET /email-log/by-doc/{doc_type}/{doc_id} — all sends for one document
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/email-log", tags=["email-log"])


_VALID_STATUSES = {"sent", "failed", "blocked", "queued"}


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a SQLAlchemy row to a JSON-safe dict."""
    return {
        "id":                   str(row.id),
        "tenant_id":            str(row.tenant_id),
        "doc_type":             row.doc_type,
        "doc_id":               str(row.doc_id),
        "doc_version":          row.doc_version,
        "sent_by_user_id":      str(row.sent_by_user_id) if row.sent_by_user_id else None,
        "from_addr":            row.from_addr,
        "to_addrs":             list(row.to_addrs) if row.to_addrs else [],
        "cc_addrs":             list(row.cc_addrs) if row.cc_addrs else [],
        "bcc_addrs":            list(row.bcc_addrs) if row.bcc_addrs else [],
        "subject":              row.subject,
        "attachment_filenames": list(row.attachment_filenames) if row.attachment_filenames else [],
        "resend_message_id":    row.resend_message_id,
        "resend_status":        row.resend_status,
        "resend_error":         row.resend_error,
        "kill_switch_reason":   row.kill_switch_reason,
        "sent_at":              row.sent_at.isoformat() if row.sent_at else None,
    }


@router.get("")
async def list_email_log(
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    doc_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    doc_id: UUID | None = Query(default=None),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
) -> dict[str, Any]:
    """Paginated list with optional filters."""
    tenant_id = resolve_tenant_id(request)

    where_clauses = ["tenant_id = :tenant_id"]
    params: dict[str, Any] = {"tenant_id": str(tenant_id), "limit": limit, "offset": offset}

    if doc_type:
        where_clauses.append("doc_type = :doc_type")
        params["doc_type"] = doc_type
    if status:
        if status not in _VALID_STATUSES:
            raise HTTPException(422, f"invalid status {status!r}")
        where_clauses.append("resend_status = :status")
        params["status"] = status
    if doc_id:
        where_clauses.append("doc_id = :doc_id")
        params["doc_id"] = str(doc_id)
    if since:
        where_clauses.append("sent_at >= :since")
        params["since"] = since
    if until:
        where_clauses.append("sent_at <= :until")
        params["until"] = until

    where = " AND ".join(where_clauses)

    total_row = await session.execute(
        text(f"SELECT COUNT(*) FROM email_send_log WHERE {where}"),
        params,
    )
    total = total_row.scalar() or 0

    rows = await session.execute(
        text(f"""
            SELECT id, tenant_id, doc_type, doc_id, doc_version,
                   sent_by_user_id, from_addr, to_addrs, cc_addrs, bcc_addrs,
                   subject, attachment_filenames,
                   resend_message_id, resend_status, resend_error,
                   kill_switch_reason, sent_at
            FROM email_send_log
            WHERE {where}
            ORDER BY sent_at DESC
            LIMIT :limit OFFSET :offset
        """),
        params,
    )
    items = [_row_to_dict(r) for r in rows]

    return {
        "items":  items,
        "total":  total,
        "limit":  limit,
        "offset": offset,
    }


@router.get("/by-doc/{doc_type}/{doc_id}")
async def list_email_log_for_doc(
    doc_type: str,
    doc_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """All send attempts for a single document, newest first."""
    tenant_id = resolve_tenant_id(request)
    rows = await session.execute(
        text("""
            SELECT id, tenant_id, doc_type, doc_id, doc_version,
                   sent_by_user_id, from_addr, to_addrs, cc_addrs, bcc_addrs,
                   subject, attachment_filenames,
                   resend_message_id, resend_status, resend_error,
                   kill_switch_reason, sent_at
            FROM email_send_log
            WHERE tenant_id = :tenant_id AND doc_type = :doc_type AND doc_id = :doc_id
            ORDER BY sent_at DESC
        """),
        {"tenant_id": str(tenant_id), "doc_type": doc_type, "doc_id": str(doc_id)},
    )
    return {"items": [_row_to_dict(r) for r in rows]}


@router.get("/{log_id}")
async def get_email_log_entry(
    log_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Single log row — includes body_html + body_text (heavy fields not in list view)."""
    tenant_id = resolve_tenant_id(request)
    row = (await session.execute(
        text("""
            SELECT id, tenant_id, doc_type, doc_id, doc_version,
                   sent_by_user_id, from_addr, to_addrs, cc_addrs, bcc_addrs,
                   subject, body_html, body_text, attachment_filenames,
                   resend_message_id, resend_status, resend_error,
                   kill_switch_reason, sent_at
            FROM email_send_log
            WHERE id = :log_id AND tenant_id = :tenant_id
        """),
        {"log_id": str(log_id), "tenant_id": str(tenant_id)},
    )).first()
    if row is None:
        raise HTTPException(404, "log entry not found")

    out = _row_to_dict(row)
    out["body_html"] = row.body_html
    out["body_text"] = row.body_text
    return out
