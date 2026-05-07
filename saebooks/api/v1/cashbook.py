"""JSON router — ``/api/v1/cashbook``.

UI-mode endpoints for the cashbook (single-entry) edition. Every
cashbook entry compiles to a real ``JournalEntry`` via
``saebooks.services.cashbook.record_cashbook_entry`` — see
``docs/cashbook-edition-design.md`` for the full design.

Phase B scope
-------------
* ``POST /entries`` — record a new cashbook entry. Idempotent via the
  required ``X-Idempotency-Key`` header.
* ``GET /entries`` — list cashbook entries (cashbook-meta filtered).
* ``GET /entries/{id}`` — single cashbook entry.
* ``GET /categories`` — picker payload for the active company,
  resolved through per-company overrides.
* ``GET /summary`` — P&L-shaped projection over cashbook lines for a
  date range.

Out of Phase B
--------------
* ``PATCH`` / ``DELETE`` (void-and-recreate) — design says "void &
  re-create, never in-place"; that's a small additional service
  surface plus tests; tracked for Phase B.5.
* Transfer endpoint (TX_TRANSFER) — needs two-bank-account flow;
  tracked separately.

Auth
----
Standard Bearer auth (``require_bearer``). All routes resolve
``tenant_id`` from the JWT claims and ``company_id`` from
``X-Company-Id`` (or first active company for the tenant via
``get_active_company_id``).

The router does NOT gate on ``bookkeeping_mode='cashbook'`` — the
service does. Calling these endpoints against a 'full'-mode company
returns ``cashbook_not_configured`` with HTTP 409. This lets a UI
probe ``GET /categories`` to decide whether to render the cashbook
surface without a separate "is this a cashbook company?" call.
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.models.company import Company
from saebooks.models.journal import EntryStatus, JournalEntry
from saebooks.services import cashbook as cashbook_svc
from saebooks.services.cashbook_categories import (
    CashbookCategory,
    UnknownCashbookCategory,
    all_defaults,
    resolve_for_company,
)

logger = logging.getLogger("saebooks.api.cashbook")


router = APIRouter(
    prefix="/cashbook",
    tags=["cashbook"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CashbookEntryCreate(BaseModel):
    """POST /entries body. Mirrors the design doc shape."""

    entry_date: date
    description: str | None = Field(default=None, max_length=500)
    amount: Decimal = Field(gt=Decimal("0"))
    direction: str = Field(pattern="^(income|expense)$")
    category_code: str = Field(min_length=1, max_length=64)
    gst_amount: Decimal | None = Field(default=None, ge=Decimal("0"))


class CashbookEntryOut(BaseModel):
    id: UUID
    journal_entry_id: UUID
    journal_entry_ref: str
    entry_date: date
    description: str | None
    amount: Decimal
    direction: str
    category_code: str
    category_label: str
    gst_amount: Decimal | None
    version: int
    created_at: str
    posted_at: str | None
    status: str


class CashbookEntryListOut(BaseModel):
    items: list[CashbookEntryOut]
    next_cursor: str | None = None


class CashbookCategoryOut(BaseModel):
    code: str
    label: str
    group: str
    direction: str
    gst_default: Decimal
    hint_text: str | None


class CashbookSummaryByCategory(BaseModel):
    code: str
    label: str
    direction: str
    amount: Decimal
    count: int


class CashbookSummaryOut(BaseModel):
    from_: date = Field(alias="from")
    to: date
    income_total: Decimal
    expense_total: Decimal
    net: Decimal
    by_category: list[CashbookSummaryByCategory]
    gst_collected: Decimal
    gst_paid: Decimal

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _category_to_out(c: CashbookCategory) -> CashbookCategoryOut:
    return CashbookCategoryOut(
        code=c.code,
        label=c.label,
        group=c.group,
        direction=c.direction,
        gst_default=c.gst_default,
        hint_text=c.hint_text,
    )


def _service_error_to_http(exc: cashbook_svc.CashbookError) -> HTTPException:
    """Map typed service errors to the public HTTP shape.

    The status code stays 409 for "not configured" (a state the client
    can fix by setting up the company), 400 for client-input errors,
    and 422 for validation. The error body always carries the stable
    ``code`` so SDKs can branch.
    """
    body = {"code": exc.code, "detail": str(exc)}
    if isinstance(exc, cashbook_svc.CashbookNotConfigured):
        return HTTPException(status.HTTP_409_CONFLICT, body)
    if isinstance(exc, cashbook_svc.CashbookCurrencyError):
        return HTTPException(status.HTTP_409_CONFLICT, body)
    if isinstance(exc, cashbook_svc.CashbookCategoryError):
        return HTTPException(status.HTTP_400_BAD_REQUEST, body)
    if isinstance(exc, cashbook_svc.CashbookAccountResolutionError):
        return HTTPException(status.HTTP_409_CONFLICT, body)
    return HTTPException(status.HTTP_400_BAD_REQUEST, body)


def _je_to_cashbook_out(je: JournalEntry) -> CashbookEntryOut:
    """Project a JE that carries cashbook_meta into the cashbook response shape."""
    meta = (je.attachments or {}).get("cashbook_meta") or {}
    direction = meta.get("direction") or "expense"
    category_code = meta.get("category_code") or ""
    try:
        category = resolve_for_company(category_code, None)
        category_label = category.label
    except UnknownCashbookCategory:
        category_label = category_code  # fall back to raw code
    gross_amount_raw = meta.get("gross_amount")
    try:
        amount = Decimal(gross_amount_raw) if gross_amount_raw else Decimal("0")
    except InvalidOperation:
        amount = Decimal("0")
    gst_raw = meta.get("gst_amount")
    try:
        gst_amount = Decimal(gst_raw) if gst_raw else None
    except InvalidOperation:
        gst_amount = None

    return CashbookEntryOut(
        id=je.id,
        journal_entry_id=je.id,
        journal_entry_ref=je.ref,
        entry_date=je.entry_date,
        description=je.description,
        amount=amount,
        direction=direction,
        category_code=category_code,
        category_label=category_label,
        gst_amount=gst_amount,
        version=je.version,
        created_at=je.created_at.isoformat() if je.created_at else "",
        posted_at=je.posted_at.isoformat() if je.posted_at else None,
        status=str(je.status),
    )


def _require_idempotency_key(value: str | None) -> str:
    if not value or not value.strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {
                "code": "idempotency_key_required",
                "detail": "X-Idempotency-Key header is required for cashbook writes",
            },
        )
    return value.strip()


# ---------------------------------------------------------------------------
# POST /entries
# ---------------------------------------------------------------------------


@router.post(
    "/entries",
    status_code=status.HTTP_201_CREATED,
    response_model=CashbookEntryOut,
)
async def create_entry(
    payload: CashbookEntryCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
) -> CashbookEntryOut:
    """Record a cashbook entry.

    Requires ``X-Idempotency-Key``: same key + same body returns the
    same JE id; same key + different body returns the originally-
    created JE (the service is amount-of-truth, not the request body)
    — see ``services.cashbook.record_cashbook_entry`` for the exact
    contract.
    """
    tenant_id = resolve_tenant_id(request)
    idem_key = _require_idempotency_key(x_idempotency_key)

    actor = getattr(request.state, "actor", None) or "api"

    try:
        je = await cashbook_svc.record_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_date=payload.entry_date,
            description=payload.description,
            amount=payload.amount,
            direction=payload.direction,  # type: ignore[arg-type]
            category_code=payload.category_code,
            gst_amount=payload.gst_amount,
            idempotency_key=idem_key,
            actor=str(actor),
        )
    except cashbook_svc.CashbookError as exc:
        raise _service_error_to_http(exc) from exc

    return _je_to_cashbook_out(je)


# ---------------------------------------------------------------------------
# GET /entries
# ---------------------------------------------------------------------------


@router.get("/entries", response_model=CashbookEntryListOut)
async def list_entries(
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = Query(default=None),
    direction: str | None = Query(default=None, pattern="^(income|expense)$"),
    category: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> CashbookEntryListOut:
    """List cashbook entries for the active company.

    Filters: date range (``from``/``to``), direction
    (income/expense), category code. ``limit`` caps the page; the
    cursor is the ``created_at`` ISO string of the last item — sole
    traders rarely have enough volume for the cursor to matter, but
    it keeps the contract honest.
    """
    stmt = (
        select(JournalEntry)
        .where(JournalEntry.company_id == company_id)
        .where(JournalEntry.attachments["cashbook_meta"].isnot(None))
    )
    if from_ is not None:
        stmt = stmt.where(JournalEntry.entry_date >= from_)
    if to is not None:
        stmt = stmt.where(JournalEntry.entry_date <= to)
    if direction is not None:
        stmt = stmt.where(
            JournalEntry.attachments["cashbook_meta"]["direction"].astext == direction
        )
    if category is not None:
        stmt = stmt.where(
            JournalEntry.attachments["cashbook_meta"]["category_code"].astext
            == category
        )
    if cursor is not None:
        stmt = stmt.where(JournalEntry.created_at < cursor)
    stmt = stmt.order_by(
        JournalEntry.entry_date.desc(),
        JournalEntry.created_at.desc(),
    ).limit(limit + 1)

    rows = (await session.execute(stmt)).scalars().unique().all()
    has_more = len(rows) > limit
    items = [_je_to_cashbook_out(je) for je in rows[:limit]]
    next_cursor = (
        rows[limit - 1].created_at.isoformat() if has_more else None
    )
    return CashbookEntryListOut(items=items, next_cursor=next_cursor)


@router.get("/entries/{entry_id}", response_model=CashbookEntryOut)
async def get_entry(
    entry_id: UUID,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> CashbookEntryOut:
    stmt = select(JournalEntry).where(
        JournalEntry.id == entry_id,
        JournalEntry.company_id == company_id,
    )
    je = (await session.execute(stmt)).scalar_one_or_none()
    if je is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cashbook entry not found")
    if not (je.attachments or {}).get("cashbook_meta"):
        # JE exists but isn't a cashbook entry — pretend it's not there to
        # keep the cashbook surface clean.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cashbook entry not found")
    return _je_to_cashbook_out(je)


# ---------------------------------------------------------------------------
# GET /categories
# ---------------------------------------------------------------------------


@router.get("/categories", response_model=list[CashbookCategoryOut])
async def list_categories(
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> list[CashbookCategoryOut]:
    """Return the picker-ready category list for the active company.

    Per-company overrides (``companies.cashbook_categories.overrides``)
    are applied. A ``hidden: true`` override drops the category from
    the list.
    """
    company = (
        await session.execute(select(Company).where(Company.id == company_id))
    ).scalar_one_or_none()
    overrides = company.cashbook_categories if company else None

    out: list[CashbookCategoryOut] = []
    for default in all_defaults():
        try:
            resolved = resolve_for_company(default.code, overrides)
        except UnknownCashbookCategory:
            continue  # hidden — skip
        out.append(_category_to_out(resolved))
    return out


# ---------------------------------------------------------------------------
# GET /summary
# ---------------------------------------------------------------------------


@router.get("/summary", response_model=CashbookSummaryOut)
async def get_summary(
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
    from_: date = Query(..., alias="from"),
    to: date = Query(...),
) -> CashbookSummaryOut:
    """P&L-shaped summary collapsed into cashbook categories.

    The summary reads cashbook-tagged JEs only. Income is the sum of
    credits to income accounts (which equal debits to bank for
    cashbook entries); expense is the sum of debits to expense
    accounts. GST totals are read from the cashbook_meta blob, not
    the GST account lines, because cashbook_meta is canonical.
    """
    if to < from_:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "to must be >= from"
        )

    stmt = (
        select(JournalEntry)
        .where(JournalEntry.company_id == company_id)
        .where(JournalEntry.entry_date >= from_)
        .where(JournalEntry.entry_date <= to)
        .where(JournalEntry.attachments["cashbook_meta"].isnot(None))
        .where(JournalEntry.status == EntryStatus.POSTED)
    )
    rows = (await session.execute(stmt)).scalars().unique().all()

    income_total = Decimal("0")
    expense_total = Decimal("0")
    gst_collected = Decimal("0")
    gst_paid = Decimal("0")
    by_category: dict[str, dict[str, Any]] = {}

    for je in rows:
        meta = (je.attachments or {}).get("cashbook_meta") or {}
        direction = meta.get("direction")
        code = meta.get("category_code") or ""
        try:
            amount = Decimal(meta.get("gross_amount") or "0")
        except InvalidOperation:
            amount = Decimal("0")
        try:
            gst = (
                Decimal(meta.get("gst_amount"))
                if meta.get("gst_amount")
                else Decimal("0")
            )
        except InvalidOperation:
            gst = Decimal("0")

        if direction == "income":
            # Net portion = amount - gst contributes to income_total
            # (gst-collected is reported separately).
            income_total += amount - gst
            gst_collected += gst
        elif direction == "expense":
            expense_total += amount - gst
            gst_paid += gst
        else:
            continue

        bucket = by_category.setdefault(
            code,
            {"code": code, "direction": direction, "amount": Decimal("0"), "count": 0},
        )
        bucket["amount"] += amount - gst
        bucket["count"] += 1

    by_cat_out: list[CashbookSummaryByCategory] = []
    for code, bucket in by_category.items():
        try:
            label = resolve_for_company(code, None).label
        except UnknownCashbookCategory:
            label = code
        by_cat_out.append(
            CashbookSummaryByCategory(
                code=code,
                label=label,
                direction=bucket["direction"],
                amount=bucket["amount"],
                count=bucket["count"],
            )
        )
    by_cat_out.sort(key=lambda x: (x.direction, -x.amount))

    return CashbookSummaryOut(
        **{
            "from": from_,
            "to": to,
            "income_total": income_total,
            "expense_total": expense_total,
            "net": income_total - expense_total,
            "by_category": by_cat_out,
            "gst_collected": gst_collected,
            "gst_paid": gst_paid,
        }
    )
