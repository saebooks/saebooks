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
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.hard_delete_gate import hard_delete_admin_gate
from saebooks.api.v1.schemas import (
    AccountConflictBody,
    AccountCreate,
    AccountListOut,
    AccountOut,
    AccountUpdate,
)
from saebooks.models.account import Account, AccountType
from saebooks.services import accounts as svc
from saebooks.services.hard_delete import hard_delete_with_audit
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
    account_type: AccountType | None = Query(default=None),
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
    company_id: UUID = Depends(get_active_company_id),
) -> AccountOut:
    tenant_id = resolve_tenant_id(request)
    account = await svc.get(session, account_id, tenant_id=tenant_id, company_id=company_id)
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

# Every ``JournalEntry.source_type`` string a posting path actually stamps
# (grepped: ``grep -rn 'source_type="' saebooks/ --include='*.py'``). Used to
# validate the ``source_type`` ledger filter (400 on anything else) — keep
# in sync when a new posting path introduces a new source_type.
_KNOWN_LEDGER_SOURCE_TYPES = frozenset({
    "bank_statement_line",
    "bill",
    "credit_note",
    "dutiable_transaction_event",
    "expense",
    "fixed_asset",
    "ic_txn",
    "invoice",
    "journal_entry",
    "payment",
    "pay_run",
    "receipt",
    "reclassification",
    "supplier_credit_note",
    "transfer",
    "trust_distribution",
})

# source_type -> model, for the subset of stamped source_types whose model
# actually carries a ``contact_id`` column (used by the ``contact_id``
# ledger filter — an OR of per-source subqueries). source_types with no
# contact linkage (transfer, pay_run, ic_txn, fixed_asset,
# bank_statement_line, journal_entry, reclassification,
# dutiable_transaction_event) are simply never matched by this filter.
# NOTE: trust_distribution is deliberately excluded — TrustDistribution
# itself has no contact_id; the per-beneficiary link lives one level down
# on BeneficiaryEntitlement (many rows per distribution, no single contact
# on the JournalEntry.source_id'd parent row), so it can't be scoped by a
# single-contact filter the same way the others can.
def _contact_linked_source_models() -> dict[str, Any]:
    from saebooks.models.bill import Bill
    from saebooks.models.credit_note import CreditNote
    from saebooks.models.expense import Expense
    from saebooks.models.invoice import Invoice
    from saebooks.models.payment import Payment
    from saebooks.models.receipt import Receipt
    from saebooks.models.supplier_credit_note import SupplierCreditNote

    return {
        "invoice": Invoice,
        "bill": Bill,
        "expense": Expense,
        "payment": Payment,
        "credit_note": CreditNote,
        "supplier_credit_note": SupplierCreditNote,
        "receipt": Receipt,
    }


def _escape_ilike(term: str) -> str:
    """Escape ``\\ % _`` so a user-supplied substring is treated literally."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/{account_id}/ledger")
async def get_account_ledger(
    account_id: UUID,
    request: Request,
    date_from: str | None = Query(default=None, description="ISO date (YYYY-MM-DD)"),
    date_to: str | None = Query(default=None, description="ISO date (YYYY-MM-DD)"),
    sort: str = Query(default="date", description="One of: date, ref, description, debit, credit"),
    direction: str = Query(default="desc", regex="^(asc|desc)$"),
    source_type: str | None = Query(
        default=None,
        description=(
            "Filter to entries stamped with this JournalEntry.source_type "
            "(e.g. 'invoice', 'bill', 'payment'). 400 on an unrecognised value."
        ),
    ),
    description: str | None = Query(
        default=None,
        description="Case-insensitive substring match on JournalLine.description.",
    ),
    contact_id: UUID | None = Query(
        default=None,
        description=(
            "Filter to entries whose source record (invoice/bill/expense/"
            "payment/credit_note/supplier_credit_note/receipt) belongs to "
            "this contact. source_types with no single-contact linkage "
            "(transfers, pay runs, trust distributions, etc.) never match."
        ),
    ),
    include_contact_name: bool = Query(
        default=False,
        description=(
            "Opt-in: attach a 'contact_name' field to each row, resolved "
            "from the source record's contact for source_types that carry "
            "one. Defaults to false to protect list performance — when "
            "true, resolved via one grouped query per source_type present "
            "on the page, never one query per row."
        ),
    ),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    """Return posted journal lines for this account with running balance.

    ``opening_balance`` reflects everything before ``date_from`` (zero
    if no date floor). Running balance follows debit/credit normality
    (credit-normal: LIABILITY/EQUITY/INCOME accumulates credit-debit;
    everything else accumulates debit-credit).

    When ``sort`` is anything other than ``date``, the running ``balance``
    field is set to ``null`` on every row — it only carries meaning when
    rows are ordered chronologically.

    When any of ``source_type``/``description``/``contact_id`` is supplied,
    both ``balance`` (every row) and ``opening_balance`` are set to ``null``
    too: those filters drop rows out of the account's real running total, so
    a computed balance would silently misrepresent the account. The
    ``total``/``limit``/``offset`` pagination and ``total_debit``/
    ``total_credit`` for the filtered page remain accurate.

    Every row also carries ``source_type`` and ``source_id`` (both copied
    from the owning ``JournalEntry``). ``contact_name`` is opt-in via
    ``include_contact_name=true`` — omitted from every row when false
    (default), to protect list performance. When enabled it's resolved via
    one grouped query per source_type present on the page (never per-row);
    ``null`` when the source has no contact or no contact linkage exists for
    that source_type.
    """
    from datetime import date as _date
    from decimal import Decimal

    from saebooks.models.contact import Contact
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

    if source_type is not None:
        source_type = source_type.lower()
        if source_type not in _KNOWN_LEDGER_SOURCE_TYPES:
            raise HTTPException(
                400,
                f"Unknown source_type {source_type!r}. Valid: {sorted(_KNOWN_LEDGER_SOURCE_TYPES)}",
            )

    description_pattern: str | None = None
    if description:
        description_pattern = f"%{_escape_ilike(description)}%"

    contact_clause = None
    if contact_id is not None:
        linked_models = _contact_linked_source_models()
        contact_clause = or_(*(
            (JournalEntry.source_type == st)
            & JournalEntry.source_id.in_(
                select(model.id).where(
                    model.contact_id == contact_id,
                    model.company_id == company_id,
                )
            )
            for st, model in linked_models.items()
        ))

    filters_active = source_type is not None or description_pattern is not None or contact_clause is not None
    # Running/opening balance only mean "the true account balance" when the
    # rows walked are exactly the account's full posted history in date
    # order — narrowing with the new filters breaks that, so we null both
    # out rather than show a number that looks authoritative but isn't
    # (same honesty convention the endpoint already applies to non-date
    # sorts, extended to cover the filtered case too).
    show_balance = (sort == "date") and not filters_active

    tenant_id = resolve_tenant_id(request)
    account = await svc.get(session, account_id, tenant_id=tenant_id, company_id=company_id)
    if account is None:
        raise HTTPException(404, "Account not found")

    credit_normal = account.account_type in _CREDIT_NORMAL

    opening_balance = Decimal("0")
    if parsed_from and not filters_active:
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
    if source_type is not None:
        count_stmt = count_stmt.where(JournalEntry.source_type == source_type)
    if description_pattern is not None:
        count_stmt = count_stmt.where(JournalLine.description.ilike(description_pattern, escape="\\"))
    if contact_clause is not None:
        count_stmt = count_stmt.where(contact_clause)
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
    if source_type is not None:
        stmt = stmt.where(JournalEntry.source_type == source_type)
    if description_pattern is not None:
        stmt = stmt.where(JournalLine.description.ilike(description_pattern, escape="\\"))
    if contact_clause is not None:
        stmt = stmt.where(contact_clause)
    stmt = stmt.limit(limit).offset(offset)

    rows = (await session.execute(stmt)).all()

    # Running balance only makes sense when sorting by date ascending with
    # no narrowing filter. For everything else, leave balance null and
    # totals are still useful.
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

    # Enrichment (opt-in via include_contact_name): resolve contact_name for
    # source_types on this page that carry a contact_id — one grouped query
    # per source_type present, never per-row. Skipped entirely when the
    # flag is off, to protect list performance.
    contact_name_by_source: dict[tuple[str, UUID], str] = {}
    if include_contact_name:
        linked_models = _contact_linked_source_models()
        ids_by_source_type: dict[str, list[UUID]] = {}
        for _jl, je in rows:
            if je.source_type in linked_models and je.source_id is not None:
                ids_by_source_type.setdefault(je.source_type, []).append(je.source_id)

        for st, ids in ids_by_source_type.items():
            model = linked_models[st]
            name_rows = (
                await session.execute(
                    select(model.id, Contact.name)
                    .join(Contact, model.contact_id == Contact.id)
                    .where(model.id.in_(ids), model.company_id == company_id)
                )
            ).all()
            for source_id, contact_name in name_rows:
                contact_name_by_source[(st, source_id)] = contact_name

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
        item = {
            "entry_id": str(je.id),
            "entry_date": je.entry_date.isoformat(),
            "ref": je.ref,
            "description": jl.description or je.description or "",
            "debit": str(jl.debit),
            "credit": str(jl.credit),
            "balance": balance_str,
            "source_type": je.source_type,
            "source_id": str(je.source_id) if je.source_id is not None else None,
        }
        if include_contact_name:
            item["contact_name"] = contact_name_by_source.get((je.source_type, je.source_id))
        items.append(item)

    body = {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "opening_balance": str(opening_balance) if not filters_active else None,
        "total_debit": str(total_debit),
        "total_credit": str(total_credit),
        "credit_normal": credit_normal,
        "sort": sort,
        "direction": direction,
    }
    return JSONResponse(body)


# ---------------------------------------------------------------------------
# GL movement (capture fact API — bank-feed reconcile variance reads this)
# ---------------------------------------------------------------------------


@router.get("/{account_id}/gl-movement")
async def get_account_gl_movement(
    account_id: UUID,
    request: Request,
    date_from: str | None = Query(default=None, description="ISO date (YYYY-MM-DD), inclusive"),
    date_to: str | None = Query(default=None, description="ISO date (YYYY-MM-DD), inclusive"),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    """Summed posted journal movement (``debit - credit``) on this account.

    This is precisely the aggregation the bank-feeds reconcile sweep needs
    to compute feed-vs-GL variance: ``SUM(debit - credit)`` over journal
    entries in status POSTED or REVERSED (so a void and its reversal
    cancel), optionally bounded by ``date_from`` / ``date_to`` on the entry
    date. The endpoint and ``services.bank_feeds.reconcile.sweep`` call the
    same ``reconcile.gl_movement`` function, so the number can't drift.

    Returns ``{account_id, date_from, date_to, movement}`` where
    ``movement`` is a decimal string (bank-statement sign convention:
    positive = money into the account).
    """
    from datetime import date as _date

    from saebooks.services.bank_feeds.reconcile import gl_movement

    def _parse(s: str | None) -> _date | None:
        if not s:
            return None
        try:
            return _date.fromisoformat(s)
        except ValueError as exc:
            raise HTTPException(400, f"Invalid date {s!r}: expected YYYY-MM-DD") from exc

    parsed_from = _parse(date_from)
    parsed_to = _parse(date_to)

    tenant_id = resolve_tenant_id(request)
    account = await svc.get(session, account_id, tenant_id=tenant_id, company_id=company_id)
    if account is None:
        raise HTTPException(404, "Account not found")

    movement = await gl_movement(
        session, account_id, date_from=parsed_from, date_to=parsed_to
    )
    return JSONResponse(
        {
            "account_id": str(account_id),
            "date_from": parsed_from.isoformat() if parsed_from else None,
            "date_to": parsed_to.isoformat() if parsed_to else None,
            "movement": str(movement),
        }
    )


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
    company_id: UUID = Depends(get_active_company_id),
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

    if await svc.get(session, account_id, tenant_id=tenant_id, company_id=company_id) is None:
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
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    if hard:
        existing = await svc.get(session, account_id, tenant_id=tenant_id, company_id=company_id)
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

    if await svc.get(session, account_id, tenant_id=tenant_id, company_id=company_id) is None:
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
