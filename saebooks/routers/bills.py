"""AP bill routes.

Public under ``/bills`` — Community-tier, no flag gate. Mirror of
``routers/invoices.py`` minus the PDF/email/sent actions (suppliers
send us their bills; we don't re-render them).

* ``GET /bills`` — list, filtered by status
* ``GET /bills/new`` + ``POST /bills`` — create draft
* ``GET /bills/{id}`` — detail view
* ``GET /bills/{id}/edit`` + ``POST /bills/{id}`` — update draft
* ``POST /bills/{id}/post`` — draft → posted (GL impact lands)
* ``POST /bills/{id}/void`` — posted → voided (reversal journal)
* ``POST /bills/{id}/archive`` — soft-delete
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
from saebooks.models.bill import BillStatus
from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.models.project import Project, ProjectStatus
from saebooks.models.tax_code import TaxCode
from saebooks.services import bills as svc
from saebooks.services import numbering

router = APIRouter(prefix="/bills")

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# Expense-side account types shown in the bill-line account picker.
_BILL_ACCOUNT_TYPES = {
    AccountType.EXPENSE,
    AccountType.COST_OF_SALES,
    AccountType.OTHER_EXPENSE,
    # Occasionally a bill hits an asset account directly (fixed asset
    # purchase, prepayment). Leaving these in lets users book those
    # without a journal entry.
    AccountType.ASSET,
}


async def _first_company() -> Company:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Company)
            .where(Company.archived_at.is_(None))
            .order_by(Company.created_at)
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
    expense_accounts = [a for a in accounts if a.account_type in _BILL_ACCOUNT_TYPES]
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
    projects = (
        await session.execute(
            select(Project)
            .where(
                Project.company_id == company_id,
                Project.archived_at.is_(None),
                Project.status == ProjectStatus.ACTIVE,
            )
            .order_by(Project.code)
        )
    ).scalars().all()
    return {
        "contacts": contacts,
        "expense_accounts": expense_accounts,
        "tax_codes": tax_codes,
        "projects": projects,
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
    """Extract ``line_<i>_<field>`` groups from the form dict."""
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
        by_idx.setdefault(int(idx_str), {})[field] = value
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
                "discount_pct": _parse_decimal(raw.get("discount_pct", "0"), "discount"),
                "project_id": uuid.UUID(raw["project_id"])
                if raw.get("project_id")
                else None,
            }
        )
    return lines


# ---------------------------------------------------------------------- #
# List                                                                    #
# ---------------------------------------------------------------------- #


@router.get("", response_class=HTMLResponse)
async def bills_list(
    request: Request,
    status: str = Query("all"),
    q: str | None = Query(None),
) -> HTMLResponse:
    company = await _first_company()
    filter_status: BillStatus | None = None
    if status.upper() in BillStatus.__members__:
        filter_status = BillStatus(status.upper())

    async with AsyncSessionLocal() as session:
        bills = await svc.list_bills(
            session,
            company.id,
            status=filter_status,
            include_archived=(status == "archived"),
        )
        contact_map: dict[uuid.UUID, str] = {}
        contact_ids = {b.contact_id for b in bills}
        if contact_ids:
            r = await session.execute(
                select(Contact.id, Contact.name).where(Contact.id.in_(contact_ids))
            )
            contact_map = {row[0]: row[1] for row in r.all()}

    if q:
        q_lower = q.lower()
        bills = [
            b
            for b in bills
            if (b.number and q_lower in b.number.lower())
            or (b.supplier_reference and q_lower in b.supplier_reference.lower())
            or q_lower in contact_map.get(b.contact_id, "").lower()
        ]
    return templates.TemplateResponse(
        request,
        "bills/list.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "bills": bills,
            "contact_map": contact_map,
            "status_filter": status,
            "search_q": q or "",
            "total": len(bills),
        },
    )


# ---------------------------------------------------------------------- #
# Create                                                                  #
# ---------------------------------------------------------------------- #


@router.get("/new", response_class=HTMLResponse)
async def bills_new(request: Request) -> HTMLResponse:
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        dropdowns = await _form_dropdowns(session, company.id)
        preview_number = await numbering.peek_next(session, company.id, "bill")
    today = date.today()
    return templates.TemplateResponse(
        request,
        "bills/form.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "bill": None,
            "today": today,
            "default_due": today,
            "preview_number": preview_number,
            "error": None,
            **dropdowns,
        },
    )


@router.post("", response_model=None)
async def bills_create(request: Request) -> RedirectResponse | HTMLResponse:
    company = await _first_company()
    form = dict(await request.form())
    try:
        kwargs: dict[str, Any] = {
            "contact_id": uuid.UUID(str(form["contact_id"])),
            "issue_date": _parse_date(str(form["issue_date"]), "issue_date"),
            "due_date": _parse_date(str(form["due_date"]), "due_date"),
            "supplier_reference": str(form.get("supplier_reference") or "").strip()
            or None,
            "notes": str(form.get("notes") or "").strip() or None,
            "lines": _parse_lines_from_form(form),
        }
        if not kwargs["lines"]:
            raise svc.BillError("At least one line with description is required")
        async with AsyncSessionLocal() as session:
            bill = await svc.create_draft(session, company_id=company.id, **kwargs)
    except (ValueError, svc.BillError) as exc:
        async with AsyncSessionLocal() as session:
            dropdowns = await _form_dropdowns(session, company.id)
            preview_number = await numbering.peek_next(session, company.id, "bill")
        return templates.TemplateResponse(
            request,
            "bills/form.html",
            {
                "edition": settings.edition,
                "company_name": company.name,
                "bill": None,
                "today": date.today(),
                "default_due": date.today(),
                "preview_number": preview_number,
                "error": str(exc),
                **dropdowns,
            },
            status_code=422,
        )
    return RedirectResponse(f"/bills/{bill.id}", status_code=303)


# ---------------------------------------------------------------------- #
# Detail / Edit                                                           #
# ---------------------------------------------------------------------- #


@router.get("/{bill_id}", response_class=HTMLResponse)
async def bills_detail(request: Request, bill_id: UUID) -> HTMLResponse:
    async with AsyncSessionLocal() as session:
        bill = await svc.get(session, bill_id)
        company = await session.get(Company, bill.company_id)
        contact = await session.get(Contact, bill.contact_id)
        account_ids = {ln.account_id for ln in bill.lines}
        tax_code_ids = {ln.tax_code_id for ln in bill.lines if ln.tax_code_id}
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
        "bills/detail.html",
        {
            "edition": settings.edition,
            "company_name": company.name if company else "",
            "bill": bill,
            "contact": contact,
            "account_map": account_map,
            "tax_map": tax_map,
        },
    )


@router.get("/{bill_id}/edit", response_class=HTMLResponse)
async def bills_edit(request: Request, bill_id: UUID) -> HTMLResponse:
    async with AsyncSessionLocal() as session:
        bill = await svc.get(session, bill_id)
        company = await session.get(Company, bill.company_id)
        dropdowns = await _form_dropdowns(session, bill.company_id)
    if bill.status != BillStatus.DRAFT:
        raise HTTPException(400, f"Cannot edit bill in state {bill.status}")
    return templates.TemplateResponse(
        request,
        "bills/form.html",
        {
            "edition": settings.edition,
            "company_name": company.name if company else "",
            "bill": bill,
            "today": date.today(),
            "default_due": bill.due_date,
            "preview_number": bill.number or "",
            "error": None,
            **dropdowns,
        },
    )


@router.post("/{bill_id}", response_model=None)
async def bills_update(
    request: Request, bill_id: UUID
) -> RedirectResponse | HTMLResponse:
    form = dict(await request.form())
    try:
        async with AsyncSessionLocal() as session:
            await svc.update_draft(
                session,
                bill_id,
                contact_id=uuid.UUID(str(form["contact_id"])),
                issue_date=_parse_date(str(form["issue_date"]), "issue_date"),
                due_date=_parse_date(str(form["due_date"]), "due_date"),
                supplier_reference=str(form.get("supplier_reference") or "").strip()
                or None,
                notes=str(form.get("notes") or "").strip() or None,
                lines=_parse_lines_from_form(form),
            )
    except (ValueError, svc.BillError) as exc:
        async with AsyncSessionLocal() as session:
            bill = await svc.get(session, bill_id)
            company = await session.get(Company, bill.company_id)
            dropdowns = await _form_dropdowns(session, bill.company_id)
        return templates.TemplateResponse(
            request,
            "bills/form.html",
            {
                "edition": settings.edition,
                "company_name": company.name if company else "",
                "bill": bill,
                "today": date.today(),
                "default_due": bill.due_date,
                "preview_number": bill.number or "",
                "error": str(exc),
                **dropdowns,
            },
            status_code=422,
        )
    return RedirectResponse(f"/bills/{bill_id}", status_code=303)


# ---------------------------------------------------------------------- #
# Actions                                                                 #
# ---------------------------------------------------------------------- #


@router.post("/{bill_id}/post")
async def bills_post(bill_id: UUID) -> RedirectResponse:
    async with AsyncSessionLocal() as session:
        await svc.post_bill(session, bill_id, posted_by="web")
    return RedirectResponse(f"/bills/{bill_id}", status_code=303)


@router.post("/{bill_id}/void")
async def bills_void(bill_id: UUID) -> RedirectResponse:
    async with AsyncSessionLocal() as session:
        await svc.void_bill(session, bill_id, posted_by="web")
    return RedirectResponse(f"/bills/{bill_id}", status_code=303)


@router.post("/{bill_id}/archive")
async def bills_archive(bill_id: UUID) -> RedirectResponse:
    async with AsyncSessionLocal() as session:
        await svc.archive(session, bill_id)
    return RedirectResponse("/bills", status_code=303)
