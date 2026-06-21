"""JSON router — ``/api/v1/bank_accounts``.

Phase 1 tier-4 bank-accounts endpoint.

Design (a): bank accounts are a view over the ``accounts`` table — rows
where ``bsb IS NOT NULL``.  No new table is needed.

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN``.
* Optimistic locking via ``If-Match: <version>`` on update/delete.
* Retry-safe writes via ``X-Idempotency-Key: <uuid>`` on POST.
* Every write appends a row to ``change_log``.
* ``DELETE`` is a soft-archive (archived_at set) returning 204.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.hard_delete_gate import hard_delete_admin_gate
from saebooks.api.v1.schemas import (
    BankAccountConflictBody,
    BankAccountCreate,
    BankAccountListOut,
    BankAccountOut,
    BankAccountUpdate,
)
from saebooks.models.bank_statement import BankStatementLine
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine
from saebooks.services import bank_accounts as svc
from saebooks.services.hard_delete import hard_delete_with_audit
from saebooks.services.idempotency import ClaimStatus, claim_or_fetch, store_response

router = APIRouter(
    prefix="/bank_accounts",
    tags=["bank_accounts"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _parse_if_match(header: str | None) -> int | None:
    if header is None:
        return None
    cleaned = header.strip().strip('"').strip("W/").strip('"')
    try:
        return int(cleaned)
    except ValueError as exc:
        raise HTTPException(
            400, f"If-Match must be an integer version, got '{header}'"
        ) from exc


def _parse_idempotency_key(header: str | None) -> str | None:
    """Return the raw idempotency key string, or None if absent."""
    if header is None or not header.strip():
        return None
    return header.strip()


def _dump(account: Any) -> dict[str, Any]:
    return json.loads(BankAccountOut.model_validate(account).model_dump_json())


def _owed_from_statement_balance(
    statement_balance: Decimal | None, account_type: str | None
) -> Decimal | None:
    """Return the positive "amount owed" implied by a statement balance.

    For credit-normal LIABILITY accounts (credit cards, loans) money spent
    drives the statement balance negative, so the amount owed is the negated
    balance. For debit-normal accounts the balance itself is the figure.
    Returns None when no statement balance is known.
    """
    if statement_balance is None:
        return None
    if account_type == "LIABILITY":
        return -statement_balance
    return statement_balance


def _apply_credit_fields(out: BankAccountOut, owed: Decimal | None) -> None:
    """Populate available + over_limit on an out model given amount owed.

    No-op (leaves both None) when the account has no credit_limit set or the
    owed figure could not be computed. available = credit_limit - owed;
    over_limit is True when owed strictly exceeds the limit.
    """
    if out.credit_limit is None or owed is None:
        return
    out.available = out.credit_limit - owed
    out.over_limit = owed > out.credit_limit


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=BankAccountListOut)
async def list_bank_accounts(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    include_balance: bool = Query(
        default=False,
        description="Include GL balance (POSTED journal lines, debit − credit) per account.",
    ),
    include_statement_balance: bool = Query(
        default=False,
        description="Include bank-statement running balance (cumulative SUM(amount) of non-archived bsls) per account.",
    ),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> BankAccountListOut:
    offset = (page - 1) * page_size
    tenant_id = resolve_tenant_id(request)
    items, total = await svc.list_active(
        session,
        company_id,
        tenant_id,
        limit=page_size,
        offset=offset,
    )

    gl_by_id: dict[UUID, Decimal] = {}
    if include_balance and items:
        bal_stmt = (
            select(
                JournalLine.account_id,
                func.coalesce(func.sum(JournalLine.debit), Decimal("0")).label("dr"),
                func.coalesce(func.sum(JournalLine.credit), Decimal("0")).label("cr"),
            )
            .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
            .where(
                JournalEntry.company_id == company_id,
                # POSTED+REVERSED so void pairs cancel (see
                # reports.REPORTABLE_STATUSES); POSTED-only double-counts voids.
                JournalEntry.status.in_((EntryStatus.POSTED, EntryStatus.REVERSED)),
            )
            .group_by(JournalLine.account_id)
        )
        for row in (await session.execute(bal_stmt)).all():
            gl_by_id[row.account_id] = Decimal(row.dr) - Decimal(row.cr)

    stmt_by_id: dict[UUID, Decimal] = {}
    if include_statement_balance and items:
        sb_stmt = (
            select(
                BankStatementLine.account_id,
                func.coalesce(func.sum(BankStatementLine.amount), Decimal("0")).label("running"),
            )
            .where(
                BankStatementLine.company_id == company_id,
                BankStatementLine.archived_at.is_(None),
            )
            .group_by(BankStatementLine.account_id)
        )
        for row in (await session.execute(sb_stmt)).all():
            stmt_by_id[row.account_id] = Decimal(row.running)

    out: list[BankAccountOut] = []
    for a in items:
        m = BankAccountOut.model_validate(a)
        if include_balance:
            m.balance = gl_by_id.get(a.id, Decimal("0"))
        if include_statement_balance:
            # None when the account has no bsls — the UI renders "—".
            m.statement_balance = stmt_by_id.get(a.id)
            # available/over_limit need the owed figure; statement balance is
            # the canonical "owed" source (matches the dashboard widget). An
            # account with a limit but no bsls is treated as owing 0.
            owed = _owed_from_statement_balance(
                m.statement_balance if m.statement_balance is not None else Decimal("0"),
                m.account_type,
            )
            _apply_credit_fields(m, owed)
        out.append(m)

    return BankAccountListOut(
        items=out,
        total=total,
        limit=page_size,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


@router.get("/{bank_account_id}", response_model=BankAccountOut)
async def get_bank_account(
    request: Request,
    bank_account_id: UUID,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> BankAccountOut:
    tenant_id = resolve_tenant_id(request)
    account = await svc.api_get(
        session, bank_account_id, tenant_id=tenant_id, company_id=company_id
    )
    if account is None:
        raise HTTPException(404, "Bank account not found")
    out = BankAccountOut.model_validate(account)
    # Compute the statement balance for this one account so the detail page
    # can show available/over_limit. Mirrors the list handler's aggregation
    # but scoped to a single account_id.
    if out.credit_limit is not None:
        sb_stmt = (
            select(
                func.coalesce(func.sum(BankStatementLine.amount), Decimal("0"))
            )
            .where(
                BankStatementLine.company_id == company_id,
                BankStatementLine.account_id == account.id,
                BankStatementLine.archived_at.is_(None),
            )
        )
        stmt_bal = Decimal((await session.execute(sb_stmt)).scalar_one())
        out.statement_balance = stmt_bal
        _apply_credit_fields(
            out, _owed_from_statement_balance(stmt_bal, out.account_type)
        )
    return out


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=BankAccountOut, status_code=201)
async def create_bank_account(
    request: Request,
    payload: BankAccountCreate,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    key = _parse_idempotency_key(idempotency_key)
    tenant_id = resolve_tenant_id(request)

    if key is not None:
        raw_body = await request.body()
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        claim = await claim_or_fetch(session, key, tenant_id, body_sha256)
        if claim.status == ClaimStatus.CONFLICT:
            return JSONResponse(
                {"code": "idempotency_key_conflict", "message": "X-Idempotency-Key reused with a different request body"},
                status_code=422,
            )
        if claim.status == ClaimStatus.IN_FLIGHT:
            return JSONResponse(
                {"code": "request_in_flight", "message": "A request with this idempotency key is currently being processed. Retry after 1 second."},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 201,
            )

    try:
        account = await svc.api_create(
            session,
            company_id,
            tenant_id,
            actor=f"api:{bearer[:8]}…",
            code=payload.code,
            name=payload.name,
            account_kind=payload.account_kind,
            bsb=payload.bsb,
            bank_account_number=payload.bank_account_number,
            bank_account_title=payload.bank_account_title,
            apca_user_id=payload.apca_user_id,
            bank_abbreviation=payload.bank_abbreviation,
            is_trust_account=payload.is_trust_account,
            credit_limit=payload.credit_limit,
            credit_limit_kind=payload.credit_limit_kind,
        )
    except (ValueError, svc.BankAccountError) as exc:
        raise HTTPException(422, str(exc)) from exc

    body = _dump(account)
    if key is not None:
        await store_response(session, key, 201, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update (PATCH with If-Match)
# ---------------------------------------------------------------------------


@router.patch(
    "/{bank_account_id}",
    responses={
        200: {"model": BankAccountOut},
        409: {"model": BankAccountConflictBody, "description": "Version mismatch"},
    },
)
async def update_bank_account(
    request: Request,
    bank_account_id: UUID,
    payload: BankAccountUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with bank account version is required")
    key = _parse_idempotency_key(idempotency_key)

    tenant_id = resolve_tenant_id(request)
    # Belt-and-braces: verify bank account belongs to this tenant + company.
    if await svc.api_get(
        session, bank_account_id, tenant_id=tenant_id, company_id=company_id
    ) is None:
        raise HTTPException(404, "Bank account not found")

    if key is not None:
        raw_body = await request.body()
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        claim = await claim_or_fetch(session, key, tenant_id, body_sha256)
        if claim.status == ClaimStatus.CONFLICT:
            return JSONResponse(
                {"code": "idempotency_key_conflict", "message": "X-Idempotency-Key reused with a different request body"},
                status_code=422,
            )
        if claim.status == ClaimStatus.IN_FLIGHT:
            return JSONResponse(
                {"code": "request_in_flight", "message": "A request with this idempotency key is currently being processed. Retry after 1 second."},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 200,
            )

    try:
        account = await svc.api_update(
            session,
            bank_account_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            **payload.model_dump(exclude_unset=True),
        )
    except svc.VersionConflict as exc:
        body = BankAccountConflictBody(
            detail="version mismatch",
            current=BankAccountOut.model_validate(exc.current),
        ).model_dump(mode="json")
        if key is not None:
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.BankAccountError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    body = _dump(account)
    if key is not None:
        await store_response(session, key, 200, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Delete (soft-archive → 204)
# ---------------------------------------------------------------------------


@router.delete(
    "/{bank_account_id}",
    responses={
        204: {"description": "Archived"},
        409: {"model": BankAccountConflictBody, "description": "Version mismatch"},
    },
)
async def delete_bank_account(
    request: Request,
    bank_account_id: UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    hard: bool = Depends(hard_delete_admin_gate),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    if hard:
        existing = await svc.api_get(
            session, bank_account_id, tenant_id=tenant_id, company_id=company_id
        )
        if existing is None:
            raise HTTPException(404, "Bank account not found")
        await hard_delete_with_audit(
            session, existing, "bank_accounts", getattr(request.state, "user", None)
        )
        await session.commit()
        return Response(status_code=204)

    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with bank account version is required")

    if await svc.api_get(
        session, bank_account_id, tenant_id=tenant_id, company_id=company_id
    ) is None:
        raise HTTPException(404, "Bank account not found")

    try:
        await svc.api_delete(
            session,
            bank_account_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
        )
    except svc.VersionConflict as exc:
        body = BankAccountConflictBody(
            detail="version mismatch",
            current=BankAccountOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.BankAccountError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    return Response(status_code=204)
