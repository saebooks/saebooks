"""Recurring-invoice routes.

Mounted at ``/invoices/recurring`` — Community-tier, no flag gate.

* ``GET /invoices/recurring`` — list, filtered by status
* ``GET /invoices/recurring/new`` + ``POST /invoices/recurring`` — create
* ``GET /invoices/recurring/{id}`` — detail + history + run-now button
* ``GET /invoices/recurring/{id}/edit`` + ``POST /invoices/recurring/{id}`` — update
* ``POST /invoices/recurring/{id}/pause|resume|end|archive`` — state transitions
* ``POST /invoices/recurring/{id}/run`` — materialise the template immediately

Route ordering matters: the literal ``/new`` path is registered before
``/{template_id}`` so FastAPI doesn't try to coerce the literal ``new``
into a UUID and 422.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import resolve_tenant_id
from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.models.invoice import Invoice
from saebooks.models.recurring_invoice import (
    RecurrenceFrequency,
    RecurrenceStatus,
)
from saebooks.models.tax_code import TaxCode
from saebooks.services import recurrence as svc
from saebooks.web import templates
from saebooks.services import active_company as active_svc

router = APIRouter(prefix="/invoices/recurring")


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
    return {
        "contacts": contacts,
        "income_accounts": income_accounts,
        "tax_codes": tax_codes,
        "frequencies": [f.value for f in RecurrenceFrequency],
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


def _parse_optional_date(raw: Any) -> date | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    return date.fromisoformat(s)


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
                "unit_price": _parse_decimal(
                    raw.get("unit_price", "0"), "unit_price"
                ),
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
async def recurring_list(
    request: Request, status: str = Query("all")
) -> HTMLResponse:
    company = await _first_company()
    filter_status: RecurrenceStatus | None = None
    if status.upper() in RecurrenceStatus.__members__:
        filter_status = RecurrenceStatus(status.upper())

    async with AsyncSessionLocal() as session:
        tmpls = await svc.list_templates(
            session,
            company.id,
            status=filter_status,
            include_archived=(status == "archived"),
        )
        contact_map: dict[uuid.UUID, str] = {}
        contact_ids = {t.contact_id for t in tmpls}
        if contact_ids:
            r = await session.execute(
                select(Contact.id, Contact.name).where(
                    Contact.id.in_(contact_ids)
                )
            )
            contact_map = {row[0]: row[1] for row in r.all()}
    return templates.TemplateResponse(
        request,
        "recurring_invoices/list.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "templates": tmpls,
            "contact_map": contact_map,
            "status_filter": status,
            "total": len(tmpls),
            "today": date.today(),
        },
    )


# ---------------------------------------------------------------------- #
# Create                                                                  #
# ---------------------------------------------------------------------- #


@router.get("/new", response_class=HTMLResponse)
async def recurring_new(request: Request) -> HTMLResponse:
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        dropdowns = await _form_dropdowns(session, company.id)
    today = date.today()
    return templates.TemplateResponse(
        request,
        "recurring_invoices/form.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "template": None,
            "today": today,
            "default_next_run": today + timedelta(days=1),
            "error": None,
            **dropdowns,
        },
    )


@router.post("", response_model=None)
async def recurring_create(
    request: Request,
) -> RedirectResponse | HTMLResponse:
    company = await _first_company()
    form = dict(await request.form())
    try:
        freq_raw = str(form.get("frequency") or "").strip().upper()
        if freq_raw not in RecurrenceFrequency.__members__:
            raise svc.RecurrenceError(f"Unknown frequency {freq_raw!r}")
        anchor_raw = str(form.get("anchor_day") or "").strip()
        anchor_day: int | None = int(anchor_raw) if anchor_raw else None
        if anchor_day is not None and not (1 <= anchor_day <= 31):
            raise svc.RecurrenceError("anchor_day must be between 1 and 31")

        kwargs: dict[str, Any] = {
            "contact_id": uuid.UUID(str(form["contact_id"])),
            "name": str(form.get("name") or "").strip()
            or "(untitled schedule)",
            "frequency": RecurrenceFrequency(freq_raw),
            "next_run": _parse_date(str(form["next_run"]), "next_run"),
            "anchor_day": anchor_day,
            "end_date": _parse_optional_date(form.get("end_date")),
            "due_days": int(
                str(form.get("due_days") or "30").strip() or "30"
            ),
            "payment_terms": str(form.get("payment_terms") or "").strip()
            or None,
            "notes": str(form.get("notes") or "").strip() or None,
            "auto_post": bool(form.get("auto_post")),
            "lines": _parse_lines_from_form(form),
        }
        if not kwargs["lines"]:
            raise svc.RecurrenceError(
                "At least one line with description is required"
            )
        async with AsyncSessionLocal() as session:
            tpl = await svc.create(session, company_id=company.id, **kwargs)
    except (ValueError, svc.RecurrenceError) as exc:
        async with AsyncSessionLocal() as session:
            dropdowns = await _form_dropdowns(session, company.id)
        return templates.TemplateResponse(
            request,
            "recurring_invoices/form.html",
            {
                "edition": settings.edition,
                "company_name": company.name,
                "template": None,
                "today": date.today(),
                "default_next_run": date.today() + timedelta(days=1),
                "error": str(exc),
                **dropdowns,
            },
            status_code=422,
        )
    return RedirectResponse(
        f"/invoices/recurring/{tpl.id}", status_code=303
    )


# ---------------------------------------------------------------------- #
# Detail                                                                  #
# ---------------------------------------------------------------------- #


@router.get("/{template_id}", response_class=HTMLResponse)
async def recurring_detail(
    request: Request, template_id: UUID
) -> HTMLResponse:
    tenant_id = resolve_tenant_id(request)
    async with AsyncSessionLocal() as session:
        try:
            tpl = await svc.get(session, template_id, tenant_id=tenant_id)
        except svc.RecurrenceError as exc:
            raise HTTPException(404, "Template not found") from exc
        company = await session.get(Company, tpl.company_id)
        contact = await session.get(Contact, tpl.contact_id)
        account_ids = {ln.account_id for ln in tpl.lines}
        tax_code_ids = {ln.tax_code_id for ln in tpl.lines if ln.tax_code_id}
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
        # Recent invoices materialised from this template: best-effort
        # match by contact_id + notes equality, since there's no FK yet.
        recent_q = (
            select(Invoice)
            .where(
                Invoice.company_id == tpl.company_id,
                Invoice.contact_id == tpl.contact_id,
            )
            .order_by(Invoice.issue_date.desc())
            .limit(10)
        )
        recent = (await session.execute(recent_q)).scalars().all()
    return templates.TemplateResponse(
        request,
        "recurring_invoices/detail.html",
        {
            "edition": settings.edition,
            "company_name": company.name if company else "",
            "template": tpl,
            "contact": contact,
            "account_map": account_map,
            "tax_map": tax_map,
            "recent_invoices": recent,
            "today": date.today(),
        },
    )


# ---------------------------------------------------------------------- #
# Edit                                                                    #
# ---------------------------------------------------------------------- #


@router.get("/{template_id}/edit", response_class=HTMLResponse)
async def recurring_edit(
    request: Request, template_id: UUID
) -> HTMLResponse:
    tenant_id = resolve_tenant_id(request)
    async with AsyncSessionLocal() as session:
        try:
            tpl = await svc.get(session, template_id, tenant_id=tenant_id)
        except svc.RecurrenceError as exc:
            raise HTTPException(404, "Template not found") from exc
        company = await session.get(Company, tpl.company_id)
        dropdowns = await _form_dropdowns(session, tpl.company_id)
    if tpl.status == RecurrenceStatus.ENDED:
        raise HTTPException(
            400, "Cannot edit an ENDED schedule; create a new one."
        )
    return templates.TemplateResponse(
        request,
        "recurring_invoices/form.html",
        {
            "edition": settings.edition,
            "company_name": company.name if company else "",
            "template": tpl,
            "today": date.today(),
            "default_next_run": tpl.next_run,
            "error": None,
            **dropdowns,
        },
    )


@router.post("/{template_id}", response_model=None)
async def recurring_update(
    request: Request, template_id: UUID
) -> RedirectResponse | HTMLResponse:
    tenant_id = resolve_tenant_id(request)
    form = dict(await request.form())
    try:
        freq_raw = str(form.get("frequency") or "").strip().upper()
        if freq_raw not in RecurrenceFrequency.__members__:
            raise svc.RecurrenceError(f"Unknown frequency {freq_raw!r}")
        anchor_raw = str(form.get("anchor_day") or "").strip()
        anchor_day: int | None = int(anchor_raw) if anchor_raw else None

        async with AsyncSessionLocal() as session:
            await svc.update(
                session,
                template_id,
                tenant_id=tenant_id,
                contact_id=uuid.UUID(str(form["contact_id"])),
                name=str(form.get("name") or "").strip() or None,
                frequency=RecurrenceFrequency(freq_raw),
                next_run=_parse_date(str(form["next_run"]), "next_run"),
                anchor_day=anchor_day,
                end_date=_parse_optional_date(form.get("end_date")),
                due_days=int(
                    str(form.get("due_days") or "30").strip() or "30"
                ),
                payment_terms=str(form.get("payment_terms") or "").strip()
                or None,
                notes=str(form.get("notes") or "").strip() or None,
                auto_post=bool(form.get("auto_post")),
                lines=_parse_lines_from_form(form),
            )
    except (ValueError, svc.RecurrenceError) as exc:
        async with AsyncSessionLocal() as session:
            try:
                tpl = await svc.get(session, template_id, tenant_id=tenant_id)
            except svc.RecurrenceError as gexc:
                raise HTTPException(404, "Template not found") from gexc
            company = await session.get(Company, tpl.company_id)
            dropdowns = await _form_dropdowns(session, tpl.company_id)
        return templates.TemplateResponse(
            request,
            "recurring_invoices/form.html",
            {
                "edition": settings.edition,
                "company_name": company.name if company else "",
                "template": tpl,
                "today": date.today(),
                "default_next_run": tpl.next_run,
                "error": str(exc),
                **dropdowns,
            },
            status_code=422,
        )
    return RedirectResponse(
        f"/invoices/recurring/{template_id}", status_code=303
    )


# ---------------------------------------------------------------------- #
# State transitions                                                       #
# ---------------------------------------------------------------------- #


@router.post("/{template_id}/pause")
async def recurring_pause(request: Request, template_id: UUID) -> RedirectResponse:
    tenant_id = resolve_tenant_id(request)
    async with AsyncSessionLocal() as session:
        try:
            await svc.pause(session, template_id, tenant_id=tenant_id)
        except svc.RecurrenceError:
            pass
    return RedirectResponse(
        f"/invoices/recurring/{template_id}", status_code=303
    )


@router.post("/{template_id}/resume")
async def recurring_resume(request: Request, template_id: UUID) -> RedirectResponse:
    tenant_id = resolve_tenant_id(request)
    async with AsyncSessionLocal() as session:
        try:
            await svc.resume(session, template_id, tenant_id=tenant_id)
        except svc.RecurrenceError:
            pass
    return RedirectResponse(
        f"/invoices/recurring/{template_id}", status_code=303
    )


@router.post("/{template_id}/end")
async def recurring_end(request: Request, template_id: UUID) -> RedirectResponse:
    tenant_id = resolve_tenant_id(request)
    async with AsyncSessionLocal() as session:
        try:
            await svc.end(session, template_id, tenant_id=tenant_id)
        except svc.RecurrenceError:
            pass
    return RedirectResponse(
        f"/invoices/recurring/{template_id}", status_code=303
    )


@router.post("/{template_id}/archive")
async def recurring_archive(request: Request, template_id: UUID) -> RedirectResponse:
    tenant_id = resolve_tenant_id(request)
    async with AsyncSessionLocal() as session:
        try:
            await svc.archive(session, template_id, tenant_id=tenant_id)
        except svc.RecurrenceError:
            pass
    return RedirectResponse("/invoices/recurring", status_code=303)


@router.post("/{template_id}/run")
async def recurring_run(request: Request, template_id: UUID) -> RedirectResponse:
    tenant_id = resolve_tenant_id(request)
    async with AsyncSessionLocal() as session:
        try:
            tpl = await svc.get(session, template_id, tenant_id=tenant_id)
        except svc.RecurrenceError as exc:
            raise HTTPException(404, "Template not found") from exc
        inv = await svc.materialise_one(session, tpl)
    return RedirectResponse(f"/invoices/{inv.id}", status_code=303)
