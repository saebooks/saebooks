"""Bank rule routes — CRUD + 'auto-apply to all unmatched' action."""
import uuid
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import settings
from saebooks.models.account import Account
from saebooks.models.bank_rule import MatchType
from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.models.tax_code import TaxCode
from saebooks.routers.deps import get_web_session
from saebooks.services import active_company as active_svc
from saebooks.services import bank_rules as svc
from saebooks.web import templates

router = APIRouter(prefix="/bank-rules")


async def _first_company() -> Company:
    return await active_svc.first_company_compat()


async def _form_dropdowns(session, company_id: uuid.UUID):
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
    return accounts, tax_codes, contacts


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def rules_list(
    request: Request,
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    company = await _first_company()
    rules = await svc.list_rules(session, company.id)
    # Resolve account names for display
    acct_ids = {r.account_id for r in rules}
    accounts = {}
    if acct_ids:
        result = await session.execute(
            select(Account).where(Account.id.in_(acct_ids))
        )
        accounts = {a.id: a for a in result.scalars().all()}

    return templates.TemplateResponse(
        request,
        "bank_rules/list.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "rules": rules,
            "accounts": accounts,
            "total": len(rules),
        },
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.get("/new", response_class=HTMLResponse)
async def rules_new(
    request: Request,
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    company = await _first_company()
    accounts, tax_codes, contacts = await _form_dropdowns(session, company.id)
    return templates.TemplateResponse(
        request,
        "bank_rules/form.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "rule": None,
            "accounts": accounts,
            "tax_codes": tax_codes,
            "contacts": contacts,
            "match_types": [m.value for m in MatchType],
            "error": None,
        },
    )


@router.post("", response_model=None)
async def rules_create(
    request: Request,
    name: str = Form(...),
    match_pattern: str = Form(...),
    match_type: str = Form("CONTAINS"),
    account_id: str = Form(...),
    tax_code: str = Form(""),
    contact_id: str = Form(""),
    description_template: str = Form(""),
    auto_create: bool = Form(False),
    priority: int = Form(0),
    is_active: bool = Form(False),
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse | HTMLResponse:
    company = await _first_company()
    try:
        await svc.create(
            session,
            company.id,
            name=name,
            match_pattern=match_pattern,
            match_type=MatchType(match_type),
            account_id=UUID(account_id),
            tax_code=tax_code or None,
            contact_id=UUID(contact_id) if contact_id else None,
            description_template=description_template or None,
            auto_create=auto_create,
            priority=priority,
            is_active=is_active,
        )
    except ValueError as exc:
        await session.rollback()
        accounts, tax_codes, contacts = await _form_dropdowns(session, company.id)
        return templates.TemplateResponse(
            request,
            "bank_rules/form.html",
            {
                "edition": settings.edition,
                "company_name": company.name,
                "rule": None,
                "accounts": accounts,
                "tax_codes": tax_codes,
                "contacts": contacts,
                "match_types": [m.value for m in MatchType],
                "error": str(exc),
            },
            status_code=422,
        )
    return RedirectResponse("/bank-rules", status_code=303)


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------


@router.get("/{rule_id}/edit", response_class=HTMLResponse)
async def rules_edit(
    request: Request,
    rule_id: UUID,
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    rule = await svc.get(session, rule_id)
    if rule is None:
        raise HTTPException(404, "Rule not found")
    company = await session.get(Company, rule.company_id)
    accounts, tax_codes, contacts = await _form_dropdowns(session, rule.company_id)
    return templates.TemplateResponse(
        request,
        "bank_rules/form.html",
        {
            "edition": settings.edition,
            "company_name": company.name if company else "",
            "rule": rule,
            "accounts": accounts,
            "tax_codes": tax_codes,
            "contacts": contacts,
            "match_types": [m.value for m in MatchType],
            "error": None,
        },
    )


@router.post("/{rule_id}", response_model=None)
async def rules_update(
    request: Request,
    rule_id: UUID,
    name: str = Form(...),
    match_pattern: str = Form(...),
    match_type: str = Form("CONTAINS"),
    account_id: str = Form(...),
    tax_code: str = Form(""),
    contact_id: str = Form(""),
    description_template: str = Form(""),
    auto_create: bool = Form(False),
    priority: int = Form(0),
    is_active: bool = Form(False),
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse | HTMLResponse:
    try:
        await svc.update(
            session, rule_id,
            name=name,
            match_pattern=match_pattern,
            match_type=MatchType(match_type),
            account_id=UUID(account_id),
            tax_code=tax_code or None,
            contact_id=UUID(contact_id) if contact_id else None,
            description_template=description_template or None,
            auto_create=auto_create,
            priority=priority,
            is_active=is_active,
            performed_by="web",
        )
    except ValueError as exc:
        await session.rollback()
        rule = await svc.get(session, rule_id)
        if rule is None:
            raise HTTPException(404, "Rule not found") from exc
        company = await session.get(Company, rule.company_id)
        accounts, tax_codes, contacts = await _form_dropdowns(session, rule.company_id)
        return templates.TemplateResponse(
            request,
            "bank_rules/form.html",
            {
                "edition": settings.edition,
                "company_name": company.name if company else "",
                "rule": rule,
                "accounts": accounts,
                "tax_codes": tax_codes,
                "contacts": contacts,
                "match_types": [m.value for m in MatchType],
                "error": str(exc),
            },
            status_code=422,
        )
    return RedirectResponse("/bank-rules", status_code=303)


@router.post("/{rule_id}/delete")
async def rules_delete(
    rule_id: UUID,
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse:
    await svc.delete(session, rule_id, performed_by="web")
    return RedirectResponse("/bank-rules", status_code=303)


# ---------------------------------------------------------------------------
# Auto-apply all rules to unmatched lines
# ---------------------------------------------------------------------------


@router.post("/run-auto", response_model=None)
async def run_auto_rules(
    request: Request,
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse:
    company = await _first_company()
    counts = await svc.auto_apply_rules(session, company.id)
    return RedirectResponse(
        f"/bank-rules?ran={counts['created']}&skipped={counts['skipped']}",
        status_code=303,
    )
