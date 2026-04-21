"""Payment routes — receipts (incoming) + supplier payments (outgoing).

Public under ``/payments``, Community-tier, no flag gate.

Shipped in Batch S:

* ``GET /payments`` — list with status + direction filters
* ``GET /payments/new`` — create form
* ``POST /payments`` — insert draft
* ``GET /payments/{id}`` — detail + allocation UI
* ``POST /payments/{id}/post`` — draft → posted (GL impact lands)
* ``POST /payments/{id}/allocate`` — attach invoice allocations
* ``POST /payments/{id}/void`` — posted → voided (reversal journal)
* ``POST /payments/{id}/archive`` — soft-delete

OUTGOING direction posts Dr AP / Cr Bank but the AP-control side
only makes sense once Bills ships in Batch V. The UI exposes the
direction field anyway so you can test round-trip without bills.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.models.invoice import Invoice, InvoiceStatus
from saebooks.models.payment import (
    PaymentDirection,
    PaymentMethod,
    PaymentStatus,
)
from saebooks.services import numbering
from saebooks.services import payments as svc
from saebooks.web import templates

router = APIRouter(prefix="/payments")


# ---------------------------------------------------------------------- #
# Helpers                                                                 #
# ---------------------------------------------------------------------- #


async def _first_company() -> Company:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
        )
        company = result.scalars().first()
        if company is None:
            raise HTTPException(500, "No active company")
        return company


async def _form_dropdowns(
    session: AsyncSession, company_id: uuid.UUID
) -> dict[str, Any]:
    contacts = (
        await session.execute(
            select(Contact)
            .where(
                Contact.company_id == company_id,
                Contact.archived_at.is_(None),
            )
            .order_by(Contact.name)
        )
    ).scalars().all()
    accounts = (
        await session.execute(
            select(Account)
            .where(
                Account.company_id == company_id,
                Account.is_header.is_(False),
                Account.archived_at.is_(None),
            )
            .order_by(Account.code)
        )
    ).scalars().all()
    # Bank is any 1-1xxx asset (current practice — contacts can tweak
    # this manually by changing the CoA).
    bank_accounts = [
        a
        for a in accounts
        if a.account_type == AccountType.ASSET
        and a.code.startswith("1-1") and a.code != "1-1200"
    ]
    return {
        "contacts": contacts,
        "bank_accounts": bank_accounts,
        "methods": list(PaymentMethod),
        "directions": list(PaymentDirection),
    }


def _parse_decimal(raw: str, field: str) -> Decimal:
    try:
        return Decimal((raw or "0").strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"Invalid number for {field}: {raw!r}") from exc


def _parse_date(raw: str, field: str) -> date:
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid date for {field}: {raw!r}") from exc


# ---------------------------------------------------------------------- #
# List                                                                    #
# ---------------------------------------------------------------------- #


@router.get("", response_class=HTMLResponse)
async def payments_list(
    request: Request,
    status: str = Query("all"),
    direction: str = Query("all"),
) -> HTMLResponse:
    company = await _first_company()
    filter_status: PaymentStatus | None = None
    if status.upper() in PaymentStatus.__members__:
        filter_status = PaymentStatus(status.upper())
    filter_direction: PaymentDirection | None = None
    if direction.upper() in PaymentDirection.__members__:
        filter_direction = PaymentDirection(direction.upper())

    async with AsyncSessionLocal() as session:
        pays = await svc.list_payments(
            session,
            company.id,
            status=filter_status,
            direction=filter_direction,
            include_archived=(status == "archived"),
        )
        contact_ids = {p.contact_id for p in pays}
        contact_map: dict[uuid.UUID, str] = {}
        if contact_ids:
            r = await session.execute(
                select(Contact.id, Contact.name).where(Contact.id.in_(contact_ids))
            )
            contact_map = {row[0]: row[1] for row in r.all()}
    return templates.TemplateResponse(
        request,
        "payments/list.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "payments": pays,
            "contact_map": contact_map,
            "status_filter": status,
            "direction_filter": direction,
            "total": len(pays),
        },
    )


# ---------------------------------------------------------------------- #
# Create                                                                  #
# ---------------------------------------------------------------------- #


@router.get("/new", response_class=HTMLResponse)
async def payments_new(request: Request) -> HTMLResponse:
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        dropdowns = await _form_dropdowns(session, company.id)
        preview_number = await numbering.peek_next(session, company.id, "payment")
    return templates.TemplateResponse(
        request,
        "payments/form.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "payment": None,
            "today": date.today(),
            "preview_number": preview_number,
            "error": None,
            **dropdowns,
        },
    )


@router.post("", response_model=None)
async def payments_create(request: Request) -> RedirectResponse | HTMLResponse:
    company = await _first_company()
    form = dict(await request.form())
    try:
        direction_raw = str(form.get("direction") or "INCOMING").upper()
        if direction_raw not in PaymentDirection.__members__:
            raise ValueError(f"Unknown direction: {direction_raw}")
        method_raw = str(form.get("method") or "eft")
        if method_raw not in PaymentMethod._value2member_map_:
            raise ValueError(f"Unknown method: {method_raw}")

        kwargs: dict[str, Any] = {
            "contact_id": uuid.UUID(str(form["contact_id"])),
            "bank_account_id": uuid.UUID(str(form["bank_account_id"])),
            "payment_date": _parse_date(str(form["payment_date"]), "payment_date"),
            "amount": _parse_decimal(str(form.get("amount", "0")), "amount"),
            "direction": PaymentDirection(direction_raw),
            "method": PaymentMethod(method_raw),
            "reference": str(form.get("reference") or "").strip() or None,
            "notes": str(form.get("notes") or "").strip() or None,
        }
        async with AsyncSessionLocal() as session:
            pay = await svc.create_draft(session, company_id=company.id, **kwargs)
    except (ValueError, svc.PaymentError) as exc:
        async with AsyncSessionLocal() as session:
            dropdowns = await _form_dropdowns(session, company.id)
            preview_number = await numbering.peek_next(
                session, company.id, "payment"
            )
        return templates.TemplateResponse(
            request,
            "payments/form.html",
            {
                "edition": settings.edition,
                "company_name": company.name,
                "payment": None,
                "today": date.today(),
                "preview_number": preview_number,
                "error": str(exc),
                **dropdowns,
            },
            status_code=422,
        )
    return RedirectResponse(f"/payments/{pay.id}", status_code=303)


# ---------------------------------------------------------------------- #
# Detail                                                                  #
# ---------------------------------------------------------------------- #


@router.get("/{payment_id}", response_class=HTMLResponse)
async def payments_detail(request: Request, payment_id: UUID) -> HTMLResponse:
    async with AsyncSessionLocal() as session:
        pay = await svc.get(session, payment_id)
        company = await session.get(Company, pay.company_id)
        contact = await session.get(Contact, pay.contact_id)
        bank = await session.get(Account, pay.bank_account_id)

        # Candidate invoices to allocate against (posted, same contact,
        # balance > 0, matching direction for incoming receipts).
        candidate_invoices: list[Invoice] = []
        if pay.direction == PaymentDirection.INCOMING:
            r = await session.execute(
                select(Invoice)
                .where(
                    Invoice.company_id == pay.company_id,
                    Invoice.contact_id == pay.contact_id,
                    Invoice.status == InvoiceStatus.POSTED,
                    Invoice.archived_at.is_(None),
                )
                .order_by(Invoice.issue_date)
            )
            candidate_invoices = [
                inv for inv in r.scalars().all()
                if (inv.total - inv.amount_paid) > Decimal("0")
            ]

        # Build an allocation map so the form can show existing amounts.
        allocated: dict[uuid.UUID, Decimal] = {
            a.invoice_id: a.amount
            for a in pay.allocations
            if a.invoice_id is not None
        }
    total_allocated = sum(allocated.values(), Decimal("0"))
    remaining = pay.amount - total_allocated
    return templates.TemplateResponse(
        request,
        "payments/detail.html",
        {
            "edition": settings.edition,
            "company_name": company.name if company else "",
            "payment": pay,
            "contact": contact,
            "bank": bank,
            "candidate_invoices": candidate_invoices,
            "allocated": allocated,
            "total_allocated": total_allocated,
            "remaining": remaining,
        },
    )


# ---------------------------------------------------------------------- #
# Actions                                                                 #
# ---------------------------------------------------------------------- #


@router.post("/{payment_id}/post")
async def payments_post(payment_id: UUID) -> RedirectResponse:
    async with AsyncSessionLocal() as session:
        await svc.post_payment(session, payment_id, posted_by="web")
    return RedirectResponse(f"/payments/{payment_id}", status_code=303)


@router.post("/{payment_id}/void")
async def payments_void(payment_id: UUID) -> RedirectResponse:
    async with AsyncSessionLocal() as session:
        await svc.void_payment(session, payment_id, posted_by="web")
    return RedirectResponse(f"/payments/{payment_id}", status_code=303)


@router.post("/{payment_id}/archive")
async def payments_archive(payment_id: UUID) -> RedirectResponse:
    async with AsyncSessionLocal() as session:
        await svc.archive(session, payment_id)
    return RedirectResponse("/payments", status_code=303)


@router.post("/{payment_id}/allocate", response_model=None)
async def payments_allocate(
    request: Request, payment_id: UUID
) -> RedirectResponse | HTMLResponse:
    """Parse ``allocate_<invoice_id>=<amount>`` pairs and replace allocations.

    Empty / zero values drop the allocation. Non-zero amounts are
    validated (positive) before calling the service.
    """
    form = dict(await request.form())
    allocations: list[tuple[uuid.UUID, Decimal]] = []
    try:
        for key, raw in form.items():
            if not key.startswith("allocate_"):
                continue
            raw_str = str(raw).strip()
            if not raw_str:
                continue
            try:
                amount = Decimal(raw_str)
            except InvalidOperation as exc:
                raise ValueError(
                    f"Invalid allocation amount for {key}: {raw!r}"
                ) from exc
            if amount <= Decimal("0"):
                continue
            try:
                inv_id = uuid.UUID(key.removeprefix("allocate_"))
            except ValueError as exc:
                raise ValueError(f"Invalid invoice id in {key}") from exc
            allocations.append((inv_id, amount))
        async with AsyncSessionLocal() as session:
            await svc.allocate(
                session,
                payment_id,
                invoice_allocations=allocations,
            )
    except (ValueError, svc.PaymentError) as exc:
        # Re-render the detail page with the error. Keep it simple —
        # just append the error as a query param the template picks up.
        return RedirectResponse(
            f"/payments/{payment_id}?allocate_error={exc}",
            status_code=303,
        )
    return RedirectResponse(f"/payments/{payment_id}", status_code=303)
