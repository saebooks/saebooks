"""Journal template routes — list, create, use, delete."""
import uuid

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.tax_code import TaxCode
from saebooks.services import journal as journal_svc
from saebooks.services import journal_templates as svc
from saebooks.web import templates
from saebooks.services import active_company as active_svc

router = APIRouter(prefix="/templates")


async def _first_company() -> Company:
    return await active_svc.first_company_compat()


@router.get("", response_class=HTMLResponse)
async def templates_list(request: Request) -> HTMLResponse:
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        tmpls = await svc.list_active(session, company.id)
    return templates.TemplateResponse(
        request,
        "templates/list.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "templates": tmpls,
        },
    )


@router.post("/from-entry/{entry_id}")
async def save_as_template(
    entry_id: uuid.UUID,
    name: str = Form(...),
    description: str = Form(""),
) -> RedirectResponse:
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        entry = await journal_svc.get(session, entry_id)
        lines = [
            {
                "account_id": str(line.account_id),
                "description": line.description or "",
                "debit": str(line.debit),
                "credit": str(line.credit),
                "tax_code_id": str(line.tax_code_id) if line.tax_code_id else "",
            }
            for line in entry.lines
        ]
        await svc.create(
            session, company.id, name=name, description=description or None, lines=lines
        )
    return RedirectResponse("/templates", status_code=303)


@router.get("/{template_id}/use", response_class=HTMLResponse)
async def use_template(request: Request, template_id: uuid.UUID) -> HTMLResponse:
    """Pre-populate the journal form from a template."""
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        tmpl = await svc.get(session, template_id)
        if tmpl is None:
            raise HTTPException(404, "Template not found")

        ref = await journal_svc.next_ref(session)

        acct_result = await session.execute(
            select(Account)
            .where(Account.company_id == company.id, Account.archived_at.is_(None))
            .order_by(Account.code)
        )
        accounts = list(acct_result.scalars().all())

        tc_result = await session.execute(
            select(TaxCode)
            .where(TaxCode.company_id == company.id, TaxCode.archived_at.is_(None))
            .order_by(TaxCode.code)
        )
        tax_codes = list(tc_result.scalars().all())

    return templates.TemplateResponse(
        request,
        "journal/form.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "entry": None,
            "ref": ref,
            "accounts": accounts,
            "tax_codes": tax_codes,
            "error": None,
            "template_lines": tmpl.lines,
            "template_name": tmpl.name,
        },
    )


@router.post("/{template_id}/delete")
async def template_delete(template_id: uuid.UUID) -> RedirectResponse:
    async with AsyncSessionLocal() as session:
        await svc.delete(session, template_id)
    return RedirectResponse("/templates", status_code=303)
