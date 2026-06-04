"""JSON router — ``/api/v1/statements``.

Supplier-statement reconciliation queue. Phase 1 (Gitea #28):
ingest + list + detail. No GL writes — ingest is review-only.

Phase 3 (Gitea #28): action endpoints — draft-missing-bill, dismiss, confirm.

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

POST /api/v1/statements/{id}/draft-missing-bill
    Draft a 0-line bill for a line with match_status=missing_in_books.
    Returns 201 with ``{"bill_id": str, "statement": StatementDetailOut}``.

POST /api/v1/statements/{id}/dismiss
    Mark statement as dismissed (not-an-AP / not-actionable).
    Returns the full StatementDetailOut.

POST /api/v1/statements/{id}/confirm
    Mark statement as reconciled (operator has reviewed).
    Returns the full StatementDetailOut.

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
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, status
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
from saebooks.models.contact import Contact, ContactType
from saebooks.models.supplier_statement import (
    StatementMatchStatus,
    StatementStatus,
    SupplierStatement,
    SupplierStatementLine,
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

# Name used for the per-company placeholder contact when statement.contact_id
# is None and we need to draft a bill.  Different from the Paperless placeholder
# ("Paperless Intake (unresolved)") so operators can filter each queue
# independently.
_STMT_PLACEHOLDER_NAME = "Statement Intake (unresolved)"


def _parse_idempotency_key(header: str | None) -> str | None:
    if header is None or not header.strip():
        return None
    return header.strip()


def _exception_count(stmt: SupplierStatement) -> int:
    """Count lines with match_status in {missing_in_books, amount_mismatch}."""
    return sum(1 for ln in stmt.lines if ln.match_status in _EXCEPTION_STATUSES)


async def _load_statement_or_404(
    session: AsyncSession,
    statement_id: UUID,
    company_id: UUID,
) -> SupplierStatement:
    """Load a SupplierStatement with its lines, checking company scope.

    Raises 404 if not found or the row belongs to a different company (the
    RLS filter makes foreign-tenant rows invisible — same contract as bills).
    """
    stmt = await session.get(
        SupplierStatement,
        statement_id,
        options=[selectinload(SupplierStatement.lines)],
    )
    if stmt is None or stmt.company_id != company_id:
        raise HTTPException(status_code=404, detail="Statement not found")
    return stmt


def _serialize_detail(stmt: SupplierStatement) -> dict[str, Any]:
    """Return a StatementDetailOut as a plain dict (JSON-serialisable)."""
    import json as _json
    out = StatementDetailOut.model_validate(stmt)
    return _json.loads(out.model_dump_json())


async def _ensure_placeholder_contact(
    session: AsyncSession,
    *,
    company_id: UUID,
    tenant_id: UUID,
) -> UUID:
    """Find or create the per-company 'Statement Intake (unresolved)' contact.

    Mirrors ``paperless_ingest._placeholder_contact`` but scoped to the
    statement reconciliation queue and using a distinct name so operators
    can distinguish the two queues.
    """
    from saebooks.services import contacts as contacts_svc

    existing = (
        await session.execute(
            select(Contact).where(
                Contact.company_id == company_id,
                Contact.name == _STMT_PLACEHOLDER_NAME,
            ).limit(1)
        )
    ).scalars().first()
    if existing is not None:
        return existing.id

    contact = await contacts_svc.create(
        session,
        company_id,
        actor="statement-recon",
        tenant_id=tenant_id,
        name=_STMT_PLACEHOLDER_NAME,
        contact_type=ContactType.SUPPLIER,
        notes=(
            "Auto-created placeholder for supplier-statement lines whose contact "
            "could not be resolved. Reassign each drafted bill to the real supplier "
            "before posting."
        ),
    )
    return contact.id


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

    # Eager-load lines for the response. ingest_statement commits internally,
    # so the stmt sits expired in the identity map; session.get() can hand back
    # that stale instance WITHOUT re-running selectinload, and `lines` then
    # lazy-loads during Pydantic serialization → MissingGreenlet (no async
    # greenlet active). Force a fresh SELECT with populate_existing so scalars
    # are refreshed and lines are genuinely eager-loaded before model_validate.
    session.expire_all()
    stmt_with_lines = (
        await session.execute(
            select(SupplierStatement)
            .where(SupplierStatement.id == stmt.id)
            .options(selectinload(SupplierStatement.lines))
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
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
    contact_id: UUID | None = Query(default=None),
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
    if contact_id is not None:
        q = q.where(SupplierStatement.contact_id == contact_id)

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
    stmt = await _load_statement_or_404(session, statement_id, company_id)
    return JSONResponse(_serialize_detail(stmt))


# ---------------------------------------------------------------------------
# POST /statements/{id}/draft-missing-bill  (Phase 3)
# ---------------------------------------------------------------------------


@router.post(
    "/{statement_id}/draft-missing-bill",
    summary="Draft a bill for a missing-in-books line",
    status_code=status.HTTP_201_CREATED,
)
async def draft_missing_bill(
    statement_id: UUID,
    request: Request,
    line_id: UUID = Body(..., embed=True),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Draft a 0-line AP bill for a line whose match_status is missing_in_books.

    The bill is created as a DRAFT — the operator must code the expense
    account(s) and post it manually. This endpoint never posts to the GL.

    Write-scope: read-only API tokens receive 403 (enforced by
    ``require_bearer`` before this handler runs).

    Args:
        line_id: UUID of the ``SupplierStatementLine`` to action. Must belong
            to this statement and have ``match_status == "missing_in_books"``.

    Returns:
        201 ``{"bill_id": "<uuid>", "statement": <StatementDetailOut>}``

    Raises:
        404: Statement not found for this tenant/company.
        422: Line not found on this statement, or its match_status is not
            ``missing_in_books``.
    """
    from saebooks.services import bills as bills_svc

    tenant_id = resolve_tenant_id(request)
    stmt = await _load_statement_or_404(session, statement_id, company_id)

    # Locate the target line on this statement.
    line: SupplierStatementLine | None = next(
        (ln for ln in stmt.lines if ln.id == line_id), None
    )
    if line is None:
        raise HTTPException(
            status_code=422,
            detail=f"Line {line_id} not found on statement {statement_id}",
        )
    if line.match_status != StatementMatchStatus.MISSING_IN_BOOKS.value:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Line {line_id} has match_status '{line.match_status}'; "
                "only 'missing_in_books' lines can be drafted"
            ),
        )

    # Resolve contact — use statement.contact_id if available, otherwise
    # find-or-create the per-company placeholder.
    if stmt.contact_id is not None:
        contact_id = stmt.contact_id
    else:
        contact_id = await _ensure_placeholder_contact(
            session, company_id=company_id, tenant_id=tenant_id
        )

    # Derive dates.
    issue_date: date = line.line_date or stmt.statement_date or date.today()
    due_date: date = issue_date + timedelta(days=30)

    notes = (
        f"Drafted from supplier statement {statement_id}, line ref {line.reference}: "
        f"amount per statement {line.amount}. "
        "Code the expense account(s) before posting."
    )

    bill = await bills_svc.create_draft(
        session,
        company_id=company_id,
        contact_id=contact_id,
        issue_date=issue_date,
        due_date=due_date,
        supplier_reference=line.reference,
        lines=None,
        notes=notes,
    )

    # Update the line: mark matched and record the draft bill id.
    line.matched_bill_id = bill.id
    line.match_status = StatementMatchStatus.MATCHED.value
    existing_note = line.note or ""
    line.note = (existing_note + " (draft bill created — code & post in Bills)").lstrip()
    await session.commit()

    # Reload the statement with its updated lines for the response.
    await session.refresh(stmt, attribute_names=["lines"])
    out = _serialize_detail(stmt)

    logger.info(
        "statements: drafted bill=%s from stmt=%s line=%s tenant=%s",
        bill.id,
        statement_id,
        line_id,
        tenant_id,
    )
    return JSONResponse({"bill_id": str(bill.id), "statement": out}, status_code=201)


# ---------------------------------------------------------------------------
# POST /statements/{id}/dismiss  (Phase 3)
# ---------------------------------------------------------------------------


@router.post(
    "/{statement_id}/dismiss",
    summary="Dismiss a supplier statement (not an AP statement / not actionable)",
)
async def dismiss_statement(
    statement_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Mark a statement as dismissed.

    Used for documents that are not AP statements or are otherwise not
    actionable (e.g. marketing material, duplicates).

    Write-scope: read-only API tokens receive 403.

    Returns:
        ``StatementDetailOut`` with ``status == "dismissed"``.

    Raises:
        404: Statement not found for this tenant/company.
    """
    stmt = await _load_statement_or_404(session, statement_id, company_id)
    stmt.status = StatementStatus.DISMISSED.value
    await session.commit()
    await session.refresh(stmt, attribute_names=["lines"])

    logger.info(
        "statements: dismissed stmt=%s tenant=%s",
        statement_id,
        resolve_tenant_id(request),
    )
    return JSONResponse(_serialize_detail(stmt))


# ---------------------------------------------------------------------------
# POST /statements/{id}/confirm  (Phase 3)
# ---------------------------------------------------------------------------


@router.post(
    "/{statement_id}/confirm",
    summary="Confirm a supplier statement as reconciled",
)
async def confirm_statement(
    statement_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Mark a statement as reconciled (operator has reviewed and signed off).

    Write-scope: read-only API tokens receive 403.

    Returns:
        ``StatementDetailOut`` with ``status == "reconciled"``.

    Raises:
        404: Statement not found for this tenant/company.
    """
    stmt = await _load_statement_or_404(session, statement_id, company_id)
    stmt.status = StatementStatus.RECONCILED.value
    await session.commit()
    await session.refresh(stmt, attribute_names=["lines"])

    logger.info(
        "statements: confirmed stmt=%s tenant=%s",
        statement_id,
        resolve_tenant_id(request),
    )
    return JSONResponse(_serialize_detail(stmt))


__all__ = ["router"]
