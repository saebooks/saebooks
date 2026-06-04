"""JSON router — ``/api/v1/statements``.

Supplier-statement reconciliation queue. Phase 1 (Gitea #28):
ingest + list + detail. No GL writes — ingest is review-only.

Endpoints
---------
POST /api/v1/statements/ingest
    Ingest a Paperless document as a supplier statement. Write-scoped
    (read-only API tokens receive 403). Returns 201 with detail shape.
    ``X-Idempotency-Key`` honoured (same 24 h replay as bills/recurring).

GET /api/v1/statements
    Queue list. Optional ``status`` filter. ``limit``/``offset`` pagination.
    Returns ``{items: [ListItem], total: int}`` ordered created_at desc.

GET /api/v1/statements/{id}
    Full detail with lines. 404 when not found (RLS makes foreign-tenant
    rows invisible — same 404 as bills).

Auth / RLS
----------
All routes: Bearer auth via ``require_bearer`` (router-level dep).
Write-scope enforcement: the ``POST /ingest`` path routes through
``require_bearer``'s API-token branch, which checks
``token_allows(scopes, "POST")`` — read-only tokens get 403 before the
handler runs. Static dev-bearer and JWT paths are unaffected (they bypass
scope logic, matching the codebase-wide convention from A2).
Tenant binding: ``get_session`` stamps ``app.current_tenant`` via the
``after_begin`` listener so FORCE-RLS on ``supplier_statements`` applies
to every query.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.schemas import (
    StatementDetailOut,
    StatementIngestRequest,
)
from saebooks.config import settings as _settings
from saebooks.models.supplier_statement import (
    StatementMatchStatus,
    SupplierStatement,
)
from saebooks.services.statements.ingest import ingest_statement

logger = logging.getLogger("saebooks.api.v1.statements")

router = APIRouter(
    prefix="/statements",
    tags=["statements"],
    dependencies=[Depends(require_bearer)],
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EXCEPTION_STATUSES = {
    StatementMatchStatus.MISSING_IN_BOOKS.value,
    StatementMatchStatus.AMOUNT_MISMATCH.value,
}


def _parse_idempotency_key(header: str | None) -> str | None:
    if header is None or not header.strip():
        return None
    return header.strip()


def _exception_count(stmt: SupplierStatement) -> int:
    """Count lines with match_status in {missing_in_books, amount_mismatch}."""
    return sum(1 for ln in stmt.lines if ln.match_status in _EXCEPTION_STATUSES)


# ---------------------------------------------------------------------------
# POST /statements/ingest
# ---------------------------------------------------------------------------


@router.post(
    "/ingest",
    summary="Ingest a Paperless document as a supplier statement",
    status_code=status.HTTP_201_CREATED,
)
async def ingest(
    payload: StatementIngestRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Ingest a Paperless document as a supplier statement.

    Calls ``ingest_statement(...)`` — fetches OCR text, extracts structured
    fields, reconciles against AP bills, and sets status via gates. Never
    posts to the GL.

    Write-scope: read-only API tokens are rejected (403) by
    ``require_bearer``'s scope enforcement before this handler runs.

    Idempotency: supply ``X-Idempotency-Key`` (any non-empty string) to make
    the call retry-safe (24 h replay window, matching the bills pattern).

    Returns:
        201 with the full ``StatementDetailOut`` shape on success or replay.
    """
    from saebooks.services.idempotency import ClaimStatus, claim_or_fetch, store_response

    tenant_id = resolve_tenant_id(request)
    key = _parse_idempotency_key(idempotency_key)

    if key is not None:
        raw_body = await request.body()
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        claim = await claim_or_fetch(session, key, tenant_id, body_sha256)
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
                    "message": "A request with this idempotency key is currently being processed. Retry after 1 second.",
                },
                status_code=503,
                headers={"Retry-After": "1"},
            )
        import json as _json
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=_json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 201,
            )

    try:
        stmt = await ingest_statement(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            paperless_document_id=payload.paperless_document_id,
            settings=_settings,
        )
        await session.commit()
    except Exception as exc:
        logger.exception(
            "statements: ingest failed tenant=%s doc=%s",
            tenant_id,
            payload.paperless_document_id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ingest failed: {exc}",
        ) from exc

    # Eager-load lines for the response (they may have been written in
    # ingest_statement; refresh the stmt within the same session).
    stmt_with_lines = await session.get(
        SupplierStatement,
        stmt.id,
        options=[selectinload(SupplierStatement.lines)],
    )
    if stmt_with_lines is None:
        # Should never happen — we just committed it.
        raise HTTPException(status_code=500, detail="Statement not found after commit")

    out = StatementDetailOut.model_validate(stmt_with_lines)
    import json as _json
    out_json = _json.loads(out.model_dump_json())

    if key is not None:
        await store_response(session, key, 201, _json.dumps(out_json))
        await session.commit()

    logger.info(
        "statements: ingested doc=%s stmt=%s status=%s tenant=%s",
        payload.paperless_document_id,
        stmt.id,
        stmt.status,
        tenant_id,
    )
    return JSONResponse(out_json, status_code=201)


# ---------------------------------------------------------------------------
# GET /statements
# ---------------------------------------------------------------------------


@router.get(
    "",
    summary="List supplier statements (queue)",
)
async def list_statements(
    request: Request,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Return the statement queue for the active company.

    Ordered by ``created_at`` desc (newest first). Optionally filter by
    ``status`` — any ``StatementStatus`` value.

    Returns:
        ``{"items": [StatementListItem], "total": int}``
    """
    q = (
        select(SupplierStatement)
        .where(SupplierStatement.company_id == company_id)
        .options(selectinload(SupplierStatement.lines))
    )
    if status_filter is not None:
        q = q.where(SupplierStatement.status == status_filter)

    count_q = select(func.count()).select_from(
        q.order_by(None).subquery()
    )
    total: int = (await session.execute(count_q)).scalar_one()

    q = q.order_by(SupplierStatement.created_at.desc()).offset(offset).limit(limit)
    rows = (await session.execute(q)).scalars().all()

    items = []
    for stmt in rows:
        items.append(
            {
                "id": str(stmt.id),
                "supplier_name": stmt.supplier_name,
                "statement_date": stmt.statement_date.isoformat() if stmt.statement_date else None,
                "status": stmt.status,
                "closing_balance": float(stmt.closing_balance) if stmt.closing_balance is not None else None,
                "our_ap_as_at": float(stmt.our_ap_as_at) if stmt.our_ap_as_at is not None else None,
                "balance_delta": float(stmt.balance_delta) if stmt.balance_delta is not None else None,
                "source_document_id": stmt.source_document_id,
                "exception_count": _exception_count(stmt),
            }
        )

    return JSONResponse({"items": items, "total": total})


# ---------------------------------------------------------------------------
# GET /statements/{id}
# ---------------------------------------------------------------------------


@router.get(
    "/{statement_id}",
    summary="Get a supplier statement with lines",
)
async def get_statement(
    statement_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Return the full detail for a single statement, including all lines.

    RLS makes foreign-tenant rows invisible — the query returns None and we
    return 404, the same contract as ``GET /bills/{id}``.

    Returns:
        ``StatementDetailOut`` on success, 404 if not found.
    """
    stmt = await session.get(
        SupplierStatement,
        statement_id,
        options=[selectinload(SupplierStatement.lines)],
    )
    # RLS: a foreign-tenant row is invisible to the session → None → 404.
    # Also catches plain "not found" with the same shape.
    if stmt is None or stmt.company_id != company_id:
        raise HTTPException(status_code=404, detail="Statement not found")

    out = StatementDetailOut.model_validate(stmt)
    import json as _json
    return JSONResponse(_json.loads(out.model_dump_json()))


__all__ = ["router"]
