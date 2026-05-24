"""Pure JSON accounts router — ``/api/v1/accounts``.

Phase 1 tier-1 entity. Follows the Phase 0 contacts pattern:

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN`` or JWT.
* Optimistic locking via ``If-Match: <version>`` on update/delete.
* Retry-safe writes via ``X-Idempotency-Key: <uuid>``.
* Every write appends a row to ``change_log`` (handled by the service layer).
* Jinja ``/accounts`` routes remain untouched — same service layer.

P0 cross-tenant leak fix
------------------------
All handlers now share a single ``Depends(get_session)`` session per
request. ``app.current_tenant`` is bound at the connection level by
``get_session``; every query is gated by the ``tenant_isolation`` RLS
policy from migration 0055. The active company is resolved by the
shared ``get_active_company_id`` dep — callers may pin a specific
company via ``X-Company-Id``; otherwise the first active company for
the tenant is used. ``svc.get`` is called with ``tenant_id`` so a
foreign-tenant UUID returns ``None`` (404) even if the row exists.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.hard_delete_gate import hard_delete_admin_gate
from saebooks.services.hard_delete import hard_delete_with_audit
from saebooks.api.v1.schemas import (
    AccountConflictBody,
    AccountCreate,
    AccountListOut,
    AccountOut,
    AccountUpdate,
)
from saebooks.models.account import Account, AccountType
from saebooks.services import accounts as svc
from saebooks.services.idempotency import ClaimStatus, claim_or_fetch, store_response

router = APIRouter(
    prefix="/accounts",
    tags=["accounts"],
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
        raise HTTPException(400, f"If-Match must be an integer version, got '{header}'") from exc


def _parse_idempotency_key(header: str | None) -> str | None:
    """Return the raw idempotency key string, or None if absent."""
    if header is None or not header.strip():
        return None
    return header.strip()


def _dump(account: Account) -> dict[str, Any]:
    return json.loads(AccountOut.model_validate(account).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=AccountListOut)
async def list_accounts(
    account_type: AccountType | None = Query(default=None),  # noqa: B008
    include_balance: bool = Query(default=False, description="Include current balance per account (POSTED journal lines)."),
    include_archived: bool = Query(default=False, description="Include accounts whose archived_at is set (historical lookups)."),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    from decimal import Decimal as _Decimal

    from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine

    count_stmt = (
        select(func.count())
        .select_from(Account)
        .where(Account.company_id == company_id)
    )
    if not include_archived:
        count_stmt = count_stmt.where(Account.archived_at.is_(None))
    if account_type is not None:
        count_stmt = count_stmt.where(Account.account_type == account_type)
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(Account)
        .where(Account.company_id == company_id)
        .order_by(Account.code)
        .offset(offset)
        .limit(limit)
    )
    if not include_archived:
        stmt = stmt.where(Account.archived_at.is_(None))
    if account_type is not None:
        stmt = stmt.where(Account.account_type == account_type)
    items = list((await session.execute(stmt)).scalars().all())

    out_items: list[dict[str, Any]] = [
        AccountOut.model_validate(a).model_dump(mode="json") for a in items
    ]

    if include_balance and items:
        # One aggregate query over journal_lines + journal_entries scoped to
        # company + POSTED. credit_normal accounts (LIAB/EQUITY/INCOME)
        # accumulate (credit - debit); everything else (debit - credit).
        bal_stmt = (
            select(
                JournalLine.account_id,
                func.coalesce(func.sum(JournalLine.debit), _Decimal("0")).label("dr"),
                func.coalesce(func.sum(JournalLine.credit), _Decimal("0")).label("cr"),
            )
            .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
            .where(
                JournalEntry.company_id == company_id,
                JournalEntry.status == EntryStatus.POSTED,
            )
            .group_by(JournalLine.account_id)
        )
        rows = (await session.execute(bal_stmt)).all()
        bal_by_id: dict[str, tuple[_Decimal, _Decimal]] = {
            str(r.account_id): (r.dr, r.cr) for r in rows
        }
        for item in out_items:
            dr, cr = bal_by_id.get(item["id"], (_Decimal("0"), _Decimal("0")))
            atype = item.get("account_type")
            credit_normal = atype in ("LIABILITY", "EQUITY", "INCOME", "OTHER_INCOME")
            bal = (cr - dr) if credit_normal else (dr - cr)
            item["balance"] = str(bal)

    return JSONResponse({
        "items": out_items,
        "total": total,
        "limit": limit,
        "offset": offset,
    })


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


@router.get("/{account_id}", response_model=AccountOut)
async def get_account(
    account_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> AccountOut:
    tenant_id = resolve_tenant_id(request)
    account = await svc.get(session, account_id, tenant_id=tenant_id)
    if account is None:
        raise HTTPException(404, "Account not found")
    return AccountOut.model_validate(account)


# ---------------------------------------------------------------------------
# Ledger (per-account GL transactions)
# ---------------------------------------------------------------------------


_CREDIT_NORMAL = frozenset({
    AccountType.LIABILITY,
    AccountType.EQUITY,
    AccountType.INCOME,
    AccountType.OTHER_INCOME,
})


@router.get("/{account_id}/ledger")
async def get_account_ledger(
    account_id: UUID,
    request: Request,
    date_from: str | None = Query(default=None, description="ISO date (YYYY-MM-DD)"),
    date_to: str | None = Query(default=None, description="ISO date (YYYY-MM-DD)"),
    sort: str = Query(default="date", description="One of: date, ref, description, debit, credit"),
    direction: str = Query(default="desc", regex="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Return posted journal lines for this account with running balance.

    ``opening_balance`` reflects everything before ``date_from`` (zero
    if no date floor). Running balance follows debit/credit normality
    (credit-normal: LIABILITY/EQUITY/INCOME accumulates credit-debit;
    everything else accumulates debit-credit).

    When ``sort`` is anything other than ``date``, the running ``balance``
    field is set to ``null`` on every row — it only carries meaning when
    rows are ordered chronologically.
    """
    from datetime import date as _date
    from decimal import Decimal

    from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine

    def _parse(s: str | None) -> _date | None:
        if not s:
            return None
        try:
            return _date.fromisoformat(s)
        except ValueError as exc:
            raise HTTPException(400, f"Invalid date {s!r}: expected YYYY-MM-DD") from exc

    parsed_from = _parse(date_from)
    parsed_to = _parse(date_to)

    _VALID_SORTS = {"date", "ref", "description", "debit", "credit"}
    if sort not in _VALID_SORTS:
        raise HTTPException(400, f"Invalid sort {sort!r}. Valid: {sorted(_VALID_SORTS)}")
    show_balance = (sort == "date")

    tenant_id = resolve_tenant_id(request)
    account = await svc.get(session, account_id, tenant_id=tenant_id)
    if account is None:
        raise HTTPException(404, "Account not found")

    credit_normal = account.account_type in _CREDIT_NORMAL

    opening_balance = Decimal("0")
    if parsed_from:
        ob_stmt = (
            select(
                func.coalesce(func.sum(JournalLine.debit), Decimal("0")).label("tot_dr"),
                func.coalesce(func.sum(JournalLine.credit), Decimal("0")).label("tot_cr"),
            )
            .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
            .where(
                JournalLine.account_id == account_id,
                JournalEntry.status == EntryStatus.POSTED,
                JournalEntry.entry_date < parsed_from,
            )
        )
        ob_row = (await session.execute(ob_stmt)).one()
        opening_balance = (
            ob_row.tot_cr - ob_row.tot_dr if credit_normal else ob_row.tot_dr - ob_row.tot_cr
        )

    # Count for pagination
    count_stmt = (
        select(func.count())
        .select_from(JournalLine)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(
            JournalLine.account_id == account_id,
            JournalEntry.status == EntryStatus.POSTED,
        )
    )
    if parsed_from:
        count_stmt = count_stmt.where(JournalEntry.entry_date >= parsed_from)
    if parsed_to:
        count_stmt = count_stmt.where(JournalEntry.entry_date <= parsed_to)
    total = (await session.execute(count_stmt)).scalar_one()

    # Map sort param to (primary, tiebreak) SQLAlchemy expressions.
    _sort_map = {
        "date":        JournalEntry.entry_date,
        "ref":         JournalEntry.ref,
        "description": JournalLine.description,
        "debit":       JournalLine.debit,
        "credit":      JournalLine.credit,
    }
    primary_col = _sort_map[sort]
    primary = primary_col.asc() if direction == "asc" else primary_col.desc()
    # Always tie-break on (entry_date, entry_id, line_no) to keep order
    # stable. When sort=date asc, that's also the natural ledger order.
    tie_date = JournalEntry.entry_date.asc() if direction == "asc" else JournalEntry.entry_date.desc()

    stmt = (
        select(JournalLine, JournalEntry)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(
            JournalLine.account_id == account_id,
            JournalEntry.status == EntryStatus.POSTED,
        )
        .order_by(
            primary,
            tie_date,
            JournalEntry.id.asc(),
            JournalLine.line_no.asc(),
        )
    )
    if parsed_from:
        stmt = stmt.where(JournalEntry.entry_date >= parsed_from)
    if parsed_to:
        stmt = stmt.where(JournalEntry.entry_date <= parsed_to)
    stmt = stmt.limit(limit).offset(offset)

    rows = (await session.execute(stmt)).all()

    # Running balance only makes sense when sorting by date ascending.
    # For everything else, leave balance null and totals are still useful.
    running = opening_balance
    if show_balance and direction == "asc" and offset > 0:
        pre_stmt = (
            select(
                func.coalesce(func.sum(JournalLine.debit), Decimal("0")).label("tot_dr"),
                func.coalesce(func.sum(JournalLine.credit), Decimal("0")).label("tot_cr"),
            )
            .select_from(JournalLine)
            .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
            .where(
                JournalLine.account_id == account_id,
                JournalEntry.status == EntryStatus.POSTED,
            )
        )
        if parsed_from:
            pre_stmt = pre_stmt.where(JournalEntry.entry_date >= parsed_from)
        if parsed_to:
            pre_stmt = pre_stmt.where(JournalEntry.entry_date <= parsed_to)
        # Subselect of all line ids in the same order, take the first `offset`.
        pre_id_stmt = (
            select(JournalLine.id)
            .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
            .where(
                JournalLine.account_id == account_id,
                JournalEntry.status == EntryStatus.POSTED,
            )
            .order_by(
                JournalEntry.entry_date.asc(),
                JournalEntry.id.asc(),
                JournalLine.line_no.asc(),
            )
            .limit(offset)
        )
        if parsed_from:
            pre_id_stmt = pre_id_stmt.where(JournalEntry.entry_date >= parsed_from)
        if parsed_to:
            pre_id_stmt = pre_id_stmt.where(JournalEntry.entry_date <= parsed_to)
        pre_stmt = pre_stmt.where(JournalLine.id.in_(pre_id_stmt.subquery().select()))
        pr = (await session.execute(pre_stmt)).one()
        running += (pr.tot_cr - pr.tot_dr) if credit_normal else (pr.tot_dr - pr.tot_cr)

    items: list[dict[str, Any]] = []
    total_debit = Decimal("0")
    total_credit = Decimal("0")
    show_running = show_balance and direction == "asc"
    for jl, je in rows:
        total_debit += jl.debit
        total_credit += jl.credit
        if show_running:
            if credit_normal:
                running += jl.credit - jl.debit
            else:
                running += jl.debit - jl.credit
            balance_str: str | None = str(running)
        else:
            balance_str = None
        items.append({
            "entry_id": str(je.id),
            "entry_date": je.entry_date.isoformat(),
            "ref": je.ref,
            "description": jl.description or je.description or "",
            "debit": str(jl.debit),
            "credit": str(jl.credit),
            "balance": balance_str,
        })

    body = {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "opening_balance": str(opening_balance),
        "total_debit": str(total_debit),
        "total_credit": str(total_credit),
        "credit_normal": credit_normal,
        "sort": sort,
        "direction": direction,
    }
    return JSONResponse(body)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=AccountOut, status_code=201)
async def create_account(
    payload: AccountCreate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    key = _parse_idempotency_key(idempotency_key)

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
        account = await svc.create(
            session,
            company_id,
            actor=f"api:{bearer[:8]}…",
            tenant_id=tenant_id,
            code=payload.code,
            name=payload.name,
            account_type=payload.account_type,
            reconcile=payload.reconcile,
            is_header=payload.is_header,
            tax_code_default=payload.tax_code_default,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    await session.refresh(account)
    body = _dump(account)
    if key is not None:
        await store_response(session, key, 201, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update (PATCH with If-Match)
# ---------------------------------------------------------------------------


@router.patch(
    "/{account_id}",
    responses={
        200: {"model": AccountOut},
        409: {"model": AccountConflictBody, "description": "Version mismatch"},
    },
)
async def update_account(
    account_id: UUID,
    payload: AccountUpdate,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with account version is required")
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
                status_code=claim.response_status or 200,
            )

    if await svc.get(session, account_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Account not found")

    try:
        account = await svc.update(
            session,
            account_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            **payload.model_dump(exclude_unset=True),
        )
    except svc.VersionConflict as exc:
        await session.refresh(exc.current)
        body = AccountConflictBody(
            detail="version mismatch",
            current=AccountOut.model_validate(exc.current),
        ).model_dump(mode="json")
        if key is not None:
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    await session.refresh(account)
    body = _dump(account)
    if key is not None:
        await store_response(session, key, 200, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Delete (soft — archive via archived_at)
# ---------------------------------------------------------------------------


@router.delete(
    "/{account_id}",
    responses={
        204: {"description": "Archived"},
        409: {"model": AccountConflictBody, "description": "Version mismatch"},
    },
)
async def archive_account(
    account_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    hard: bool = Depends(hard_delete_admin_gate),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    if hard:
        existing = await svc.get(session, account_id, tenant_id=tenant_id)
        if existing is None:
            raise HTTPException(404, "Account not found")
        await hard_delete_with_audit(
            session, existing, "accounts", getattr(request.state, "user", None)
        )
        await session.commit()
        return Response(status_code=204)

    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with account version is required")
    key = _parse_idempotency_key(idempotency_key)

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
                status_code=claim.response_status or 204,
            )

    if await svc.get(session, account_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Account not found")

    try:
        account = await svc.archive(
            session,
            account_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
        )
    except svc.VersionConflict as exc:
        await session.refresh(exc.current)
        body = AccountConflictBody(
            detail="version mismatch",
            current=AccountOut.model_validate(exc.current),
        ).model_dump(mode="json")
        if key is not None:
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    if account is None:
        raise HTTPException(404, "Account not found")
    if key is not None:
        archived_body = json.dumps({"archived": str(account.id)}).encode()
        await store_response(session, key, 204, archived_body)
        await session.commit()
    return Response(status_code=204)
