"""JSON router — ``/api/v1/reconciliation``.

Phase 1 cycle 42.

Reconciliation endpoints — matching bank statement lines to posted journal
entries.  The underlying CRUD for bank statement lines lives at
``/api/v1/bank_statement_lines``; these endpoints expose the *matching*
operations built on top of ``services/reconciliation.py``.

The active company is resolved by the shared ``get_active_company_id``
dep — callers may pin a specific company via ``X-Company-Id``;
otherwise the first active company for the tenant is used.

Endpoints
---------
GET  /api/v1/reconciliation/accounts
    List reconcilable bank/cash accounts (asset, reconcile=True, not archived).

GET  /api/v1/reconciliation/unmatched
    List unmatched bank statement lines for a given account.
    Required query param: ``account_id`` (UUID).

GET  /api/v1/reconciliation/suggest/{bsl_id}
    Suggest candidate posted journal entries that could match a BSL.
    Each candidate carries ``confidence`` (HIGH/MEDIUM/LOW), ``match_reason``
    (EXACT_AMOUNT/AMOUNT_AND_DATE/AMOUNT_AND_REFERENCE/RULE_PATTERN) and a
    nullable ``rule_id`` (R8a — additive fields, candidates are still
    exact-amount matches under the hood).

POST /api/v1/reconciliation/match
    Match a BSL to a specific journal entry.
    Body: ``{"bsl_id": "<uuid>", "entry_id": "<uuid>"}``

POST /api/v1/reconciliation/unmatch/{bsl_id}
    Remove a match from a BSL, returning it to UNMATCHED status.

POST /api/v1/reconciliation/auto_match
    Run "honest" automatic matching for all unmatched BSLs in a given account
    (R8d). Required query param: ``account_id`` (UUID). Links a line ONLY
    when exactly one candidate scores HIGH confidence; never posts anything.
    Returns ``{"matched": N, "skipped_ambiguous": M, "skipped_no_candidate": K}``.

POST /api/v1/reconciliation/create_and_match
    Compound op (R8c): create a record (expense or payment), post it, and
    match it to a bank statement line in one call. See
    ``services/reconciliation.create_and_match`` for the atomicity notes.
    Body: ``{"bsl_id": "<uuid>", "record_type": "expense"|"payment",
    "expense": {...} | "payment": {...}}``
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.schemas import ExpenseLineCreate, PaymentAllocationCreate
from saebooks.models.bank_statement import BankStatementLine, StatementLineStatus
from saebooks.services import reconciliation as svc
from saebooks.services.authz import no_additional_gate, require_permission_or_role

router = APIRouter(
    prefix="/reconciliation",
    tags=["reconciliation"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Pydantic schemas (local — reconciliation-specific)
# ---------------------------------------------------------------------------


class AccountSummary(BaseModel):
    id: str
    code: str
    name: str


class BslSummary(BaseModel):
    id: str
    account_id: str
    txn_date: str
    description: str | None
    amount: str
    reference: str | None
    status: str


class EntrySummary(BaseModel):
    id: str
    ref: str
    entry_date: str
    description: str | None
    status: str
    # R8a — additive scoring fields.
    confidence: str
    match_reason: str
    rule_id: str | None = None


class MatchRequest(BaseModel):
    bsl_id: UUID
    entry_id: UUID


class AutoMatchResult(BaseModel):
    matched: int
    skipped_ambiguous: int
    skipped_no_candidate: int


# ---------------------------------------------------------------------------
# Compound create-and-match schemas (R8c)
# ---------------------------------------------------------------------------


class CreateAndMatchExpenseSpec(BaseModel):
    """Expense side of a create_and_match body.

    ``payment_account_id``, ``amount`` and direction are NEVER accepted
    here — they are always derived from the bank statement line
    server-side, so the created expense and the match can never disagree.
    """

    contact_id: UUID | None = None
    expense_date: date | None = None
    reference: str | None = None
    notes: str | None = None
    currency: str = Field(default="AUD", min_length=3, max_length=3)
    fx_rate: Decimal | None = None
    lines: list[ExpenseLineCreate] = Field(default_factory=list)


class CreateAndMatchPaymentSpec(BaseModel):
    """Payment side of a create_and_match body.

    ``bank_account_id``, ``amount`` and ``direction`` are NEVER accepted
    here — see ``CreateAndMatchExpenseSpec`` docstring.
    """

    contact_id: UUID
    payment_date: date | None = None
    method: str = "eft"
    reference: str | None = None
    notes: str | None = None
    currency: str = Field(default="AUD", min_length=3, max_length=3)
    fx_rate: Decimal | None = None
    allocations: list[PaymentAllocationCreate] = Field(default_factory=list)


class CreateAndMatchRequest(BaseModel):
    bsl_id: UUID
    record_type: Literal["expense", "payment"]
    expense: CreateAndMatchExpenseSpec | None = None
    payment: CreateAndMatchPaymentSpec | None = None

    @model_validator(mode="after")
    def _spec_matches_record_type(self) -> CreateAndMatchRequest:
        if self.record_type == "expense" and self.expense is None:
            raise ValueError("record_type=expense requires an 'expense' spec")
        if self.record_type == "payment" and self.payment is None:
            raise ValueError("record_type=payment requires a 'payment' spec")
        return self


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _dump_bsl(line: BankStatementLine) -> dict[str, Any]:
    return {
        "id": str(line.id),
        "account_id": str(line.account_id),
        "txn_date": str(line.txn_date),
        "description": line.description,
        "amount": str(line.amount),
        "reference": line.reference,
        "status": str(line.status),
        "matched_entry_id": str(line.matched_entry_id) if line.matched_entry_id else None,
        "matched_at": line.matched_at.isoformat() if line.matched_at else None,
        "matched_by": line.matched_by,
    }


def _dump_entry(
    entry: Any, confidence: str, match_reason: str, rule_id: UUID | None
) -> dict[str, Any]:
    return {
        "id": str(entry.id),
        "ref": entry.ref,
        "entry_date": str(entry.entry_date),
        "description": entry.description,
        "status": str(entry.status),
        "confidence": confidence,
        "match_reason": match_reason,
        "rule_id": str(rule_id) if rule_id else None,
    }


# ---------------------------------------------------------------------------
# GET /reconciliation/accounts
# ---------------------------------------------------------------------------


@router.get("/accounts")
async def list_reconciliation_accounts(
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """List bank/cash accounts eligible for reconciliation.

    Returns accounts where account_type=ASSET and reconcile=True.
    """
    accounts = await svc.bank_accounts(session, company_id)

    return JSONResponse(
        [{"id": str(a.id), "code": a.code, "name": a.name} for a in accounts]
    )


# ---------------------------------------------------------------------------
# GET /reconciliation/unmatched
# ---------------------------------------------------------------------------


@router.get("/unmatched")
async def list_unmatched_lines(
    account_id: UUID = Query(..., description="Bank account UUID to list unmatched lines for"),
    limit: int | None = Query(default=None, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """List unmatched bank statement lines for an account.

    Without ``limit`` the full unmatched set is returned (legacy shape).
    The response body stays a bare array either way; ``X-Total-Count``
    carries the unpaginated total for pagination UIs.
    """
    lines = await svc.statement_lines(
        session,
        company_id,
        account_id,
        status=StatementLineStatus.UNMATCHED,
        limit=limit,
        offset=offset,
    )
    total = await svc.count_statement_lines(
        session,
        company_id,
        account_id,
        status=StatementLineStatus.UNMATCHED,
    )

    return JSONResponse(
        [_dump_bsl(ln) for ln in lines],
        headers={"X-Total-Count": str(total)},
    )


# ---------------------------------------------------------------------------
# GET /reconciliation/suggest/{bsl_id}
# ---------------------------------------------------------------------------


@router.get("/suggest/{bsl_id}")
async def suggest_matches(
    bsl_id: UUID,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Suggest candidate posted journal entries that could match a BSL.

    Uses exact-amount matching against the bank account: a deposit (positive)
    looks for an entry with a debit of that amount to the bank account; a
    withdrawal (negative) looks for a credit.
    """
    stmt_line = await session.get(BankStatementLine, bsl_id)
    if stmt_line is None or stmt_line.archived_at is not None:
        raise HTTPException(404, "Bank statement line not found")
    if stmt_line.company_id != company_id:
        raise HTTPException(404, "Bank statement line not found")

    candidates = await svc.candidate_entries(
        session,
        company_id,
        stmt_line.account_id,
        stmt_line,
    )

    scored = []
    for entry in candidates:
        confidence, match_reason, rule_id = await svc.score_candidate(
            session, company_id, stmt_line, entry
        )
        scored.append(_dump_entry(entry, confidence, match_reason, rule_id))

    return JSONResponse(scored)


# ---------------------------------------------------------------------------
# POST /reconciliation/match
# ---------------------------------------------------------------------------


@router.post(
    "/match",
    dependencies=[
        Depends(require_permission_or_role("reconciliation.match", no_additional_gate))
    ],
)
async def match_line(
    payload: MatchRequest,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Match a bank statement line to a posted journal entry.

    Sets the BSL status to MATCHED and records the matched_entry_id.
    """
    # Tenant isolation — verify BSL belongs to this company
    stmt_line = await session.get(BankStatementLine, payload.bsl_id)
    if stmt_line is None or stmt_line.archived_at is not None:
        raise HTTPException(404, "Bank statement line not found")
    if stmt_line.company_id != company_id:
        raise HTTPException(404, "Bank statement line not found")

    try:
        updated = await svc.match_line(session, payload.bsl_id, payload.entry_id)
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    return JSONResponse(_dump_bsl(updated))


# ---------------------------------------------------------------------------
# POST /reconciliation/unmatch/{bsl_id}
# ---------------------------------------------------------------------------


@router.post(
    "/unmatch/{bsl_id}",
    dependencies=[
        Depends(require_permission_or_role("reconciliation.unmatch", no_additional_gate))
    ],
)
async def unmatch_line(
    bsl_id: UUID,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Remove a match from a bank statement line, returning it to UNMATCHED."""
    # Tenant isolation
    stmt_line = await session.get(BankStatementLine, bsl_id)
    if stmt_line is None or stmt_line.archived_at is not None:
        raise HTTPException(404, "Bank statement line not found")
    if stmt_line.company_id != company_id:
        raise HTTPException(404, "Bank statement line not found")

    try:
        updated = await svc.unmatch_line(session, bsl_id)
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    return JSONResponse(_dump_bsl(updated))


# ---------------------------------------------------------------------------
# POST /reconciliation/auto_match
# ---------------------------------------------------------------------------


@router.post(
    "/auto_match",
    dependencies=[
        Depends(require_permission_or_role("reconciliation.match", no_additional_gate))
    ],
)
async def auto_match(
    account_id: UUID = Query(..., description="Bank account UUID to auto-match"),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Run "honest" automatic matching for all unmatched BSLs in an account (R8d).

    For each unmatched line, candidates are scored (see
    ``services/reconciliation.score_candidate``) and a link is made ONLY
    when exactly one candidate scores HIGH confidence — ambiguous lines
    (2+ HIGH candidates) are skipped and counted, not guessed at. Never
    posts anything — only links already-POSTED entries (unchanged
    invariant from before R8).

    Returns ``{"matched": N, "skipped_ambiguous": M, "skipped_no_candidate": K}``.
    """
    result = await svc.auto_match(session, company_id, account_id)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# POST /reconciliation/create_and_match
# ---------------------------------------------------------------------------


@router.post(
    "/create_and_match",
    dependencies=[
        Depends(require_permission_or_role("reconciliation.match", no_additional_gate))
    ],
)
async def create_and_match(
    payload: CreateAndMatchRequest,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Compound op (R8c): create a record, post it, and match it to a BSL.

    ``record_type`` selects which record-type engine creates the record —
    ``expense`` (a withdrawal only) or ``payment`` (either direction).
    The record's account/amount/direction are always derived from the bank
    statement line itself, never accepted from the caller, so the created
    record and the resulting match can never disagree.

    See ``services/reconciliation.create_and_match`` for exactly what is
    and isn't atomic across the create → post → match pipeline.
    """
    tenant_id = resolve_tenant_id(request)

    expense_spec = payload.expense.model_dump() if payload.expense else None
    if expense_spec is not None:
        expense_spec["lines"] = [line.model_dump() for line in payload.expense.lines]

    payment_spec = payload.payment.model_dump() if payload.payment else None
    if payment_spec is not None:
        payment_spec["allocations"] = [
            a.model_dump() for a in payload.payment.allocations
        ]

    try:
        result = await svc.create_and_match(
            session,
            payload.bsl_id,
            company_id=company_id,
            tenant_id=tenant_id,
            actor=f"api:{bearer[:8]}…",
            record_type=payload.record_type,
            expense_spec=expense_spec,
            payment_spec=payment_spec,
        )
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    return JSONResponse(
        {
            "bsl": _dump_bsl(result["bsl"]),
            "record_type": result["record_type"],
            "record_id": str(result["record_id"]),
            "journal_entry_id": str(result["journal_entry_id"]),
            "match_id": str(result["match_id"]),
        },
        status_code=201,
    )
