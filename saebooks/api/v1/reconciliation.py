"""JSON router — ``/api/v1/reconciliation``.

Phase 1 cycle 42.

Reconciliation endpoints — matching bank statement lines to posted journal
entries.  The underlying CRUD for bank statement lines lives at
``/api/v1/bank_statement_lines``; these endpoints expose the *matching*
operations built on top of ``services/reconciliation.py``.

Endpoints
---------
GET  /api/v1/reconciliation/accounts
    List reconcilable bank/cash accounts (asset, reconcile=True, not archived).

GET  /api/v1/reconciliation/unmatched
    List unmatched bank statement lines for a given account.
    Required query param: ``account_id`` (UUID).

GET  /api/v1/reconciliation/suggest/{bsl_id}
    Suggest candidate posted journal entries that could match a BSL.
    Returns a list of entry summaries with no confidence score (service does
    exact-amount matching).

POST /api/v1/reconciliation/match
    Match a BSL to a specific journal entry.
    Body: ``{"bsl_id": "<uuid>", "entry_id": "<uuid>"}``

POST /api/v1/reconciliation/unmatch/{bsl_id}
    Remove a match from a BSL, returning it to UNMATCHED status.

POST /api/v1/reconciliation/auto_match
    Run automatic matching for all unmatched BSLs in a given account.
    Required query param: ``account_id`` (UUID).
    For each unmatched line, takes the first candidate entry and matches it.
    Returns ``{"matched": N}`` where N is the number of lines matched.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from saebooks.api.v1.auth import require_bearer
from saebooks.db import AsyncSessionLocal
from saebooks.models.bank_statement import BankStatementLine, StatementLineStatus
from saebooks.models.company import Company
from saebooks.services import reconciliation as svc

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


class MatchRequest(BaseModel):
    bsl_id: UUID
    entry_id: UUID


class AutoMatchResult(BaseModel):
    matched: int


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


async def _first_company_id(session) -> UUID:
    """Return the first active company — Phase 1 single-company assumption."""
    result = await session.execute(
        select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
    )
    company = result.scalars().first()
    if company is None:
        raise HTTPException(500, "No active company")
    return company.id


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


def _dump_entry(entry: Any) -> dict[str, Any]:
    return {
        "id": str(entry.id),
        "ref": entry.ref,
        "entry_date": str(entry.entry_date),
        "description": entry.description,
        "status": str(entry.status),
    }


# ---------------------------------------------------------------------------
# GET /reconciliation/accounts
# ---------------------------------------------------------------------------


@router.get("/accounts")
async def list_reconciliation_accounts() -> Any:
    """List bank/cash accounts eligible for reconciliation.

    Returns accounts where account_type=ASSET and reconcile=True.
    """
    async with AsyncSessionLocal() as session:
        company_id = await _first_company_id(session)
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
) -> Any:
    """List unmatched bank statement lines for an account."""
    async with AsyncSessionLocal() as session:
        company_id = await _first_company_id(session)
        lines = await svc.statement_lines(
            session,
            company_id,
            account_id,
            status=StatementLineStatus.UNMATCHED,
        )

    return JSONResponse([_dump_bsl(l) for l in lines])


# ---------------------------------------------------------------------------
# GET /reconciliation/suggest/{bsl_id}
# ---------------------------------------------------------------------------


@router.get("/suggest/{bsl_id}")
async def suggest_matches(bsl_id: UUID) -> Any:
    """Suggest candidate posted journal entries that could match a BSL.

    Uses exact-amount matching against the bank account: a deposit (positive)
    looks for an entry with a debit of that amount to the bank account; a
    withdrawal (negative) looks for a credit.
    """
    async with AsyncSessionLocal() as session:
        company_id = await _first_company_id(session)

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

    return JSONResponse([_dump_entry(e) for e in candidates])


# ---------------------------------------------------------------------------
# POST /reconciliation/match
# ---------------------------------------------------------------------------


@router.post("/match")
async def match_line(payload: MatchRequest) -> Any:
    """Match a bank statement line to a posted journal entry.

    Sets the BSL status to MATCHED and records the matched_entry_id.
    """
    async with AsyncSessionLocal() as session:
        company_id = await _first_company_id(session)

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


@router.post("/unmatch/{bsl_id}")
async def unmatch_line(bsl_id: UUID) -> Any:
    """Remove a match from a bank statement line, returning it to UNMATCHED."""
    async with AsyncSessionLocal() as session:
        company_id = await _first_company_id(session)

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


@router.post("/auto_match")
async def auto_match(
    account_id: UUID = Query(..., description="Bank account UUID to auto-match"),
) -> Any:
    """Run automatic matching for all unmatched BSLs in an account.

    For each unmatched line, finds candidate journal entries using exact-amount
    matching and matches the line to the first (earliest) candidate found.
    Returns ``{"matched": N}``.
    """
    matched = 0

    async with AsyncSessionLocal() as session:
        company_id = await _first_company_id(session)

        unmatched = await svc.statement_lines(
            session,
            company_id,
            account_id,
            status=StatementLineStatus.UNMATCHED,
        )

        for line in unmatched:
            candidates = await svc.candidate_entries(
                session,
                company_id,
                account_id,
                line,
            )
            if not candidates:
                continue
            try:
                await svc.match_line(session, line.id, candidates[0].id)
                matched += 1
            except ValueError:
                # Entry already matched or line state changed — skip
                continue

    return JSONResponse({"matched": matched})
