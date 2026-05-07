"""AR invoice routes.

Public under ``/invoices`` — Community-tier, no flag gate.

* ``GET /invoices`` — list, filtered by status
* ``GET /invoices/new`` + ``POST /invoices`` — create draft
* ``GET /invoices/{id}`` — detail view
* ``GET /invoices/{id}/edit`` + ``POST /invoices/{id}`` — update draft
* ``POST /invoices/{id}/post`` — draft → posted (GL impact lands)
* ``POST /invoices/{id}/void`` — posted → voided (reversal journal)
* ``POST /invoices/{id}/sent`` — mark sent (timestamps only)
* ``POST /invoices/{id}/archive`` — soft-delete
* ``GET /invoices/{id}.pdf`` — rendered tax invoice
* ``POST /invoices/{id}/email`` — send PDF + HTML to contact email
"""
from __future__ import annotations

import contextlib
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import resolve_tenant_id
from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.models.invoice import InvoiceStatus
from saebooks.models.project import Project, ProjectStatus
from saebooks.models.tax_code import TaxCode
from saebooks.services import invoices as svc
from saebooks.services import mailer as mailer_svc
from saebooks.services import numbering
from saebooks.services import pdf as pdf_svc
from saebooks.web import templates
from saebooks.services import active_company as active_svc

router = APIRouter(prefix="/invoices")


# ---------------------------------------------------------------------- #
# Helpers                                                                 #
# ---------------------------------------------------------------------- #


async def _first_company() -> Company:
    return await active_svc.first_company_compat()


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
        "income_accounts": income_accounts,
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
        ssd_raw = (raw.get("service_start_date") or "").strip()
        sed_raw = (raw.get("service_end_date") or "").strip()
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
                "service_start_date": ssd_raw or None,
                "service_end_date": sed_raw or None,
                "retention_pct": _parse_decimal(
                    raw.get("retention_pct", "0"), "retention_pct"
                ),
            }
        )
    return lines


# ---------------------------------------------------------------------- #
# List                                                                    #
# ---------------------------------------------------------------------- #


@router.get("", response_class=HTMLResponse)
async def invoices_list(
    request: Request,
    status: str = Query("all"),
    q: str | None = Query(None),
) -> HTMLResponse:
    company = await _first_company()
    filter_status: InvoiceStatus | None = None
    if status.upper() in InvoiceStatus.__members__:
        filter_status = InvoiceStatus(status.upper())

    async with AsyncSessionLocal() as session:
        invoices = await svc.list_invoices(
            session,
            company.id,
            status=filter_status,
            include_archived=(status == "archived"),
        )
        # Contact names
        contact_map: dict[uuid.UUID, str] = {}
        contact_ids = {inv.contact_id for inv in invoices}
        if contact_ids:
            r = await session.execute(
                select(Contact.id, Contact.name).where(Contact.id.in_(contact_ids))
            )
            contact_map = {row[0]: row[1] for row in r.all()}

    # naive in-python q filter
    if q:
        q_lower = q.lower()
        invoices = [
            inv
            for inv in invoices
            if (inv.number and q_lower in inv.number.lower())
            or q_lower in contact_map.get(inv.contact_id, "").lower()
        ]
    return templates.TemplateResponse(
        request,
        "invoices/list.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "invoices": invoices,
            "contact_map": contact_map,
            "status_filter": status,
            "search_q": q or "",
            "total": len(invoices),
        },
    )


# ---------------------------------------------------------------------- #
# Create                                                                  #
# ---------------------------------------------------------------------- #


@router.get("/new", response_class=HTMLResponse)
async def invoices_new(request: Request) -> HTMLResponse:
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        dropdowns = await _form_dropdowns(session, company.id)
        preview_number = await numbering.peek_next(session, company.id, "invoice")
    today = date.today()
    return templates.TemplateResponse(
        request,
        "invoices/form.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "invoice": None,
            "today": today,
            "default_due": today,
            "preview_number": preview_number,
            "error": None,
            **dropdowns,
        },
    )


@router.post("", response_model=None)
async def invoices_create(request: Request) -> RedirectResponse | HTMLResponse:
    company = await _first_company()
    form = dict(await request.form())
    try:
        sd_raw = str(form.get("settlement_date") or "").strip()
        kwargs: dict[str, Any] = {
            "contact_id": uuid.UUID(str(form["contact_id"])),
            "issue_date": _parse_date(str(form["issue_date"]), "issue_date"),
            "due_date": _parse_date(str(form["due_date"]), "due_date"),
            "settlement_date": _parse_date(sd_raw, "settlement_date") if sd_raw else None,
            "notes": str(form.get("notes") or "").strip() or None,
            "payment_terms": str(form.get("payment_terms") or "").strip() or None,
            "lines": _parse_lines_from_form(form),
        }
        if not kwargs["lines"]:
            raise svc.InvoiceError("At least one line with description is required")
        async with AsyncSessionLocal() as session:
            inv = await svc.create_draft(session, company_id=company.id, **kwargs)
    except (ValueError, svc.InvoiceError) as exc:
        async with AsyncSessionLocal() as session:
            dropdowns = await _form_dropdowns(session, company.id)
            preview_number = await numbering.peek_next(session, company.id, "invoice")
        return templates.TemplateResponse(
            request,
            "invoices/form.html",
            {
                "edition": settings.edition,
                "company_name": company.name,
                "invoice": None,
                "today": date.today(),
                "default_due": date.today(),
                "preview_number": preview_number,
                "error": str(exc),
                **dropdowns,
            },
            status_code=422,
        )
    return RedirectResponse(f"/invoices/{inv.id}", status_code=303)


# ---------------------------------------------------------------------- #
# Detail                                                                  #
# ---------------------------------------------------------------------- #


@router.get("/{invoice_id}.pdf")
async def invoices_pdf(invoice_id: UUID) -> Response:
    # Registered BEFORE ``/{invoice_id}`` so FastAPI's order-sensitive
    # dispatcher doesn't swallow ``<uuid>.pdf`` into the detail handler
    # and 422 on UUID coercion.
    ctx = await _render_invoice_context(invoice_id)
    data = pdf_svc.render_invoice_pdf(ctx)
    filename = f"{ctx.get('number', 'invoice')}.pdf"
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.get("/{invoice_id}", response_class=HTMLResponse)
async def invoices_detail(request: Request, invoice_id: UUID) -> HTMLResponse:
    tenant_id = resolve_tenant_id(request)
    async with AsyncSessionLocal() as session:
        try:
            inv = await svc.get(session, invoice_id, tenant_id=tenant_id)
        except svc.InvoiceError as exc:
            raise HTTPException(404, "Invoice not found") from exc
        company = await session.get(Company, inv.company_id)
        contact = await session.get(Contact, inv.contact_id)
        # Resolve account + tax-code names for the line display
        account_ids = {ln.account_id for ln in inv.lines}
        tax_code_ids = {ln.tax_code_id for ln in inv.lines if ln.tax_code_id}
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
        "invoices/detail.html",
        {
            "edition": settings.edition,
            "company_name": company.name if company else "",
            "invoice": inv,
            "contact": contact,
            "account_map": account_map,
            "tax_map": tax_map,
        },
    )


# ---------------------------------------------------------------------- #
# Edit                                                                    #
# ---------------------------------------------------------------------- #


@router.get("/{invoice_id}/edit", response_class=HTMLResponse)
async def invoices_edit(request: Request, invoice_id: UUID) -> HTMLResponse:
    tenant_id = resolve_tenant_id(request)
    async with AsyncSessionLocal() as session:
        try:
            inv = await svc.get(session, invoice_id, tenant_id=tenant_id)
        except svc.InvoiceError as exc:
            raise HTTPException(404, "Invoice not found") from exc
        company = await session.get(Company, inv.company_id)
        dropdowns = await _form_dropdowns(session, inv.company_id)
    if inv.status != InvoiceStatus.DRAFT:
        raise HTTPException(400, f"Cannot edit invoice in state {inv.status}")
    return templates.TemplateResponse(
        request,
        "invoices/form.html",
        {
            "edition": settings.edition,
            "company_name": company.name if company else "",
            "invoice": inv,
            "today": date.today(),
            "default_due": inv.due_date,
            "preview_number": inv.number or "",
            "error": None,
            **dropdowns,
        },
    )


@router.post("/{invoice_id}", response_model=None)
async def invoices_update(
    request: Request, invoice_id: UUID
) -> RedirectResponse | HTMLResponse:
    tenant_id = resolve_tenant_id(request)
    form = dict(await request.form())
    try:
        sd_raw_u = str(form.get("settlement_date") or "").strip()
        async with AsyncSessionLocal() as session:
            await svc.update_draft(
                session,
                invoice_id,
                tenant_id=tenant_id,
                contact_id=uuid.UUID(str(form["contact_id"])),
                issue_date=_parse_date(str(form["issue_date"]), "issue_date"),
                due_date=_parse_date(str(form["due_date"]), "due_date"),
                settlement_date=_parse_date(sd_raw_u, "settlement_date") if sd_raw_u else None,
                notes=str(form.get("notes") or "").strip() or None,
                payment_terms=str(form.get("payment_terms") or "").strip() or None,
                lines=_parse_lines_from_form(form),
            )
    except (ValueError, svc.InvoiceError) as exc:
        async with AsyncSessionLocal() as session:
            try:
                inv = await svc.get(session, invoice_id, tenant_id=tenant_id)
            except svc.InvoiceError:
                raise HTTPException(404, "Invoice not found") from exc
            company = await session.get(Company, inv.company_id)
            dropdowns = await _form_dropdowns(session, inv.company_id)
        return templates.TemplateResponse(
            request,
            "invoices/form.html",
            {
                "edition": settings.edition,
                "company_name": company.name if company else "",
                "invoice": inv,
                "today": date.today(),
                "default_due": inv.due_date,
                "preview_number": inv.number or "",
                "error": str(exc),
                **dropdowns,
            },
            status_code=422,
        )
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


# ---------------------------------------------------------------------- #
# Actions — post / void / sent / archive                                  #
# ---------------------------------------------------------------------- #


@router.post("/{invoice_id}/post")
async def invoices_post(invoice_id: UUID) -> RedirectResponse:
    async with AsyncSessionLocal() as session:
        await svc.post_invoice(session, invoice_id, posted_by="web")
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


@router.post("/{invoice_id}/void")
async def invoices_void(invoice_id: UUID) -> RedirectResponse:
    async with AsyncSessionLocal() as session:
        await svc.void_invoice(session, invoice_id, posted_by="web")
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


@router.post("/{invoice_id}/sent")
async def invoices_sent(invoice_id: UUID) -> RedirectResponse:
    async with AsyncSessionLocal() as session:
        await svc.mark_sent(session, invoice_id)
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


@router.post("/{invoice_id}/archive")
async def invoices_archive(
    request: Request, invoice_id: UUID
) -> RedirectResponse:
    tenant_id = resolve_tenant_id(request)
    async with AsyncSessionLocal() as session:
        try:
            await svc.archive(session, invoice_id, tenant_id=tenant_id)
        except svc.InvoiceError:
            # Cross-tenant archive — silently no-op (303 to list per
            # forum#2 acceptance criteria; row stays untouched).
            pass
    return RedirectResponse("/invoices", status_code=303)


# ---------------------------------------------------------------------- #
# PDF + email                                                             #
# ---------------------------------------------------------------------- #


async def _render_invoice_context(invoice_id: uuid.UUID) -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        inv = await svc.get(session, invoice_id)
        company = await session.get(Company, inv.company_id)
        contact = await session.get(Contact, inv.contact_id)
        # Tax-code labels for the PDF line grid.
        tax_code_ids = {ln.tax_code_id for ln in inv.lines if ln.tax_code_id}
        tax_map: dict[uuid.UUID, TaxCode] = {}
        if tax_code_ids:
            r2 = await session.execute(
                select(TaxCode).where(TaxCode.id.in_(tax_code_ids))
            )
            tax_map = {t.id: t for t in r2.scalars().all()}

    lines_ctx = []
    for ln in inv.lines:
        tax = tax_map.get(ln.tax_code_id) if ln.tax_code_id else None
        lines_ctx.append(
            {
                "description": ln.description,
                "quantity": f"{ln.quantity:g}",
                "unit_price": f"{ln.unit_price:,.2f}",
                "tax_label": tax.code if tax else "—",
                "line_total": f"{ln.line_total:,.2f}",
            }
        )

    def _addr_lines(obj: Any) -> list[str]:
        if obj is None:
            return []
        bits: list[str] = []
        if getattr(obj, "address_line1", None):
            bits.append(str(obj.address_line1))
        if getattr(obj, "address_line2", None):
            bits.append(str(obj.address_line2))
        loc = " ".join(
            s for s in (
                getattr(obj, "city", None),
                getattr(obj, "state", None),
                getattr(obj, "postcode", None),
            )
            if s
        ).strip()
        if loc:
            bits.append(loc)
        return bits

    ctx: dict[str, Any] = {
        "kind": "Tax Invoice",
        "number": inv.number or "(draft)",
        "issue_date": inv.issue_date.isoformat(),
        "due_date": inv.due_date.isoformat(),
        "company": {
            "name": company.name if company else "",
            "abn": getattr(company, "abn", None),
            "address_lines": _addr_lines(company),
        },
        "contact": {
            "name": contact.name if contact else "",
            "abn": getattr(contact, "abn", None),
            "address_lines": _addr_lines(contact),
        },
        "lines": lines_ctx,
        "subtotal": f"{inv.subtotal:,.2f}",
        "tax_total": f"{inv.tax_total:,.2f}",
        "total": f"{inv.total:,.2f}",
        "amount_paid": f"{inv.amount_paid:,.2f}",
        "balance_due": f"{(inv.total - inv.amount_paid):,.2f}",
        "notes": inv.notes,
        "payment_terms": inv.payment_terms,
    }
    return ctx


@router.post("/{invoice_id}/email")
async def invoices_email(
    invoice_id: UUID,
    to: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
) -> RedirectResponse:
    ctx = await _render_invoice_context(invoice_id)
    pdf_bytes = pdf_svc.render_invoice_pdf(ctx)
    async with AsyncSessionLocal() as session:
        inv = await svc.get(session, invoice_id)
        contact = await session.get(Contact, inv.contact_id)
    recipient = to.strip() or (contact.email if contact and contact.email else "")
    if not recipient:
        raise HTTPException(400, "No recipient email (contact has none and none supplied)")

    subject = subject.strip() or f"Invoice {ctx['number']}"
    html = body.strip() or (
        f"<p>Hi {ctx['contact']['name']},</p>"
        f"<p>Please find attached tax invoice {ctx['number']} "
        f"for ${ctx['total']} due {ctx['due_date']}.</p>"
        f"<p>Thanks, {ctx['company']['name']}</p>"
    )
    await mailer_svc.send_email(
        recipient,
        subject,
        html,
        attachments=[
            mailer_svc.EmailAttachment(
                f"{ctx['number']}.pdf", pdf_bytes, "application/pdf"
            )
        ],
    )
    async with AsyncSessionLocal() as session:
        inv = await svc.get(session, invoice_id)
        if inv.status == InvoiceStatus.POSTED:
            with contextlib.suppress(svc.InvoiceError):
                await svc.mark_sent(session, invoice_id)
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)
