"""Credit-note routes — seller-issued refunds / returns.

Public under ``/credit-notes``, Community-tier, no flag gate.

Shipped in Batch S:

* ``GET /credit-notes`` — list
* ``GET /credit-notes/new`` + ``POST /credit-notes`` — create draft
* ``GET /credit-notes/{id}`` — detail
* ``GET /credit-notes/{id}/edit`` + ``POST /credit-notes/{id}`` — update
* ``POST /credit-notes/{id}/post`` — draft → posted (reverse-sign journal)
* ``POST /credit-notes/{id}/void`` — posted → voided (reversal journal)
* ``POST /credit-notes/{id}/archive`` — soft-delete

Allocation against invoices lands in a later batch — for now a credit
note simply reduces AR by posting a reverse-sign journal, and the
amount_allocated column stays at zero.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.models.credit_note import CreditNoteStatus
from saebooks.models.tax_code import TaxCode
from saebooks.services import credit_notes as svc
from saebooks.services import numbering

router = APIRouter(prefix="/credit-notes")

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


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
    income_accounts = [
        a
        for a in accounts
        if a.account_type in (AccountType.INCOME, AccountType.OTHER_INCOME)
    ]
    tax_codes = (
        await session.execute(
            select(TaxCode)
            .where(
                TaxCode.company_id == company_id,
                TaxCode.archived_at.is_(None),
            )
            .order_by(TaxCode.code)
        )
    ).scalars().all()
    return {
        "contacts": contacts,
        "income_accounts": income_accounts,
        "tax_codes": tax_codes,
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


def _parse_lines_from_form(form: dict[str, Any]) -> list[dict[str, object]]:
    by_idx: dict[int, dict[str, str]] = {}
    for key, value in form.items():
        if not key.startswith("line_"):
            continue
        parts = key.split("_", 2)
        if len(parts) != 3:
            continue
        _, idx_str, field = parts
        if not idx_str.isdigit():
            continue
        by_idx.setdefault(int(idx_str), {})[field] = str(value)
    lines: list[dict[str, object]] = []
    for idx in sorted(by_idx):
        raw = by_idx[idx]
        desc = (raw.get("description") or "").strip()
        if not desc:
            continue
        lines.append(
            {
                "description": desc,
                "account_id": uuid.UUID(raw["account_id"]),
                "tax_code_id": uuid.UUID(raw["tax_code_id"])
                if raw.get("tax_code_id")
                else None,
                "quantity": _parse_decimal(raw.get("quantity", "1"), "quantity"),
                "unit_price": _parse_decimal(raw.get("unit_price", "0"), "unit_price"),
                "discount_pct": _parse_decimal(
                    raw.get("discount_pct", "0"), "discount"
                ),
            }
        )
    return lines


# ---------------------------------------------------------------------- #
# List                                                                    #
# ---------------------------------------------------------------------- #


@router.get("", response_class=HTMLResponse)
async def credit_notes_list(
    request: Request,
    status: str = Query("all"),
) -> HTMLResponse:
    company = await _first_company()
    filter_status: CreditNoteStatus | None = None
    if status.upper() in CreditNoteStatus.__members__:
        filter_status = CreditNoteStatus(status.upper())
    async with AsyncSessionLocal() as session:
        notes = await svc.list_credit_notes(
            session,
            company.id,
            status=filter_status,
            include_archived=(status == "archived"),
        )
        contact_ids = {cn.contact_id for cn in notes}
        contact_map: dict[uuid.UUID, str] = {}
        if contact_ids:
            r = await session.execute(
                select(Contact.id, Contact.name).where(Contact.id.in_(contact_ids))
            )
            contact_map = {row[0]: row[1] for row in r.all()}
    return templates.TemplateResponse(
        request,
        "credit_notes/list.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "credit_notes": notes,
            "contact_map": contact_map,
            "status_filter": status,
            "total": len(notes),
        },
    )


# ---------------------------------------------------------------------- #
# Create                                                                  #
# ---------------------------------------------------------------------- #


@router.get("/new", response_class=HTMLResponse)
async def credit_notes_new(request: Request) -> HTMLResponse:
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        dropdowns = await _form_dropdowns(session, company.id)
        preview_number = await numbering.peek_next(
            session, company.id, "credit_note"
        )
    return templates.TemplateResponse(
        request,
        "credit_notes/form.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "credit_note": None,
            "today": date.today(),
            "preview_number": preview_number,
            "error": None,
            **dropdowns,
        },
    )


@router.post("", response_model=None)
async def credit_notes_create(
    request: Request,
) -> RedirectResponse | HTMLResponse:
    company = await _first_company()
    form = dict(await request.form())
    try:
        kwargs: dict[str, Any] = {
            "contact_id": uuid.UUID(str(form["contact_id"])),
            "issue_date": _parse_date(str(form["issue_date"]), "issue_date"),
            "reason": str(form.get("reason") or "").strip() or None,
            "notes": str(form.get("notes") or "").strip() or None,
            "lines": _parse_lines_from_form(form),
        }
        original = str(form.get("original_invoice_id") or "").strip()
        if original:
            kwargs["original_invoice_id"] = uuid.UUID(original)
        if not kwargs["lines"]:
            raise svc.CreditNoteError(
                "At least one line with description is required"
            )
        async with AsyncSessionLocal() as session:
            cn = await svc.create_draft(session, company_id=company.id, **kwargs)
    except (ValueError, svc.CreditNoteError) as exc:
        async with AsyncSessionLocal() as session:
            dropdowns = await _form_dropdowns(session, company.id)
            preview_number = await numbering.peek_next(
                session, company.id, "credit_note"
            )
        return templates.TemplateResponse(
            request,
            "credit_notes/form.html",
            {
                "edition": settings.edition,
                "company_name": company.name,
                "credit_note": None,
                "today": date.today(),
                "preview_number": preview_number,
                "error": str(exc),
                **dropdowns,
            },
            status_code=422,
        )
    return RedirectResponse(f"/credit-notes/{cn.id}", status_code=303)


# ---------------------------------------------------------------------- #
# Detail                                                                  #
# ---------------------------------------------------------------------- #


@router.get("/{credit_note_id}", response_class=HTMLResponse)
async def credit_notes_detail(
    request: Request, credit_note_id: UUID
) -> HTMLResponse:
    async with AsyncSessionLocal() as session:
        cn = await svc.get(session, credit_note_id)
        company = await session.get(Company, cn.company_id)
        contact = await session.get(Contact, cn.contact_id)
        account_ids = {ln.account_id for ln in cn.lines}
        tax_code_ids = {ln.tax_code_id for ln in cn.lines if ln.tax_code_id}
        account_map: dict[uuid.UUID, Account] = {}
        tax_map: dict[uuid.UUID, TaxCode] = {}
        if account_ids:
            r = await session.execute(
                select(Account).where(Account.id.in_(account_ids))
            )
            account_map = {a.id: a for a in r.scalars().all()}
        if tax_code_ids:
            r2 = await session.execute(
                select(TaxCode).where(TaxCode.id.in_(tax_code_ids))
            )
            tax_map = {t.id: t for t in r2.scalars().all()}
    return templates.TemplateResponse(
        request,
        "credit_notes/detail.html",
        {
            "edition": settings.edition,
            "company_name": company.name if company else "",
            "credit_note": cn,
            "contact": contact,
            "account_map": account_map,
            "tax_map": tax_map,
        },
    )


# ---------------------------------------------------------------------- #
# Edit                                                                    #
# ---------------------------------------------------------------------- #


@router.get("/{credit_note_id}/edit", response_class=HTMLResponse)
async def credit_notes_edit(
    request: Request, credit_note_id: UUID
) -> HTMLResponse:
    async with AsyncSessionLocal() as session:
        cn = await svc.get(session, credit_note_id)
        company = await session.get(Company, cn.company_id)
        dropdowns = await _form_dropdowns(session, cn.company_id)
    if cn.status != CreditNoteStatus.DRAFT:
        raise HTTPException(400, f"Cannot edit credit note in state {cn.status}")
    return templates.TemplateResponse(
        request,
        "credit_notes/form.html",
        {
            "edition": settings.edition,
            "company_name": company.name if company else "",
            "credit_note": cn,
            "today": date.today(),
            "preview_number": cn.number or "",
            "error": None,
            **dropdowns,
        },
    )


@router.post("/{credit_note_id}", response_model=None)
async def credit_notes_update(
    request: Request, credit_note_id: UUID
) -> RedirectResponse | HTMLResponse:
    form = dict(await request.form())
    try:
        async with AsyncSessionLocal() as session:
            await svc.update_draft(
                session,
                credit_note_id,
                contact_id=uuid.UUID(str(form["contact_id"])),
                issue_date=_parse_date(
                    str(form["issue_date"]), "issue_date"
                ),
                reason=str(form.get("reason") or "").strip() or None,
                notes=str(form.get("notes") or "").strip() or None,
                lines=_parse_lines_from_form(form),
            )
    except (ValueError, svc.CreditNoteError) as exc:
        async with AsyncSessionLocal() as session:
            cn = await svc.get(session, credit_note_id)
            company = await session.get(Company, cn.company_id)
            dropdowns = await _form_dropdowns(session, cn.company_id)
        return templates.TemplateResponse(
            request,
            "credit_notes/form.html",
            {
                "edition": settings.edition,
                "company_name": company.name if company else "",
                "credit_note": cn,
                "today": date.today(),
                "preview_number": cn.number or "",
                "error": str(exc),
                **dropdowns,
            },
            status_code=422,
        )
    return RedirectResponse(f"/credit-notes/{credit_note_id}", status_code=303)


# ---------------------------------------------------------------------- #
# Actions                                                                 #
# ---------------------------------------------------------------------- #


@router.post("/{credit_note_id}/post")
async def credit_notes_post(credit_note_id: UUID) -> RedirectResponse:
    async with AsyncSessionLocal() as session:
        await svc.post_credit_note(session, credit_note_id, posted_by="web")
    return RedirectResponse(f"/credit-notes/{credit_note_id}", status_code=303)


@router.post("/{credit_note_id}/void")
async def credit_notes_void(credit_note_id: UUID) -> RedirectResponse:
    async with AsyncSessionLocal() as session:
        await svc.void_credit_note(session, credit_note_id, posted_by="web")
    return RedirectResponse(f"/credit-notes/{credit_note_id}", status_code=303)


@router.post("/{credit_note_id}/archive")
async def credit_notes_archive(credit_note_id: UUID) -> RedirectResponse:
    async with AsyncSessionLocal() as session:
        await svc.archive(session, credit_note_id)
    return RedirectResponse("/credit-notes", status_code=303)
