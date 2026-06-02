"""Trust distribution routes.

/distributions              — list all distributions
/distributions/new          — GET form, POST create
/distributions/{id}         — detail + post-JE action
/distributions/{id}/minute  — record resolution minute date
/distributions/{id}/post    — create + post the GL journal entry
/distributions/{id}/delete  — soft delete
/year-end                   — alias redirect to /distributions
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import settings
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.routers.deps import get_web_session
from saebooks.services import distributions as svc
from saebooks.web import templates
from saebooks.services import active_company as active_svc

router = APIRouter()

_EQUITY_TYPES = {AccountType.EQUITY}


async def _first_company() -> Company:
    return await active_svc.first_company_compat()


async def _all_accounts(session: AsyncSession, company_id: uuid.UUID) -> list[Account]:
    result = await session.execute(
        select(Account)
        .where(Account.company_id == company_id, Account.archived_at.is_(None))
        .order_by(Account.code)
    )
    return list(result.scalars().all())


@router.get("/year-end")
async def year_end_redirect() -> RedirectResponse:
    return RedirectResponse("/distributions", status_code=302)


@router.get("/distributions", response_class=HTMLResponse)
async def distributions_list(
    request: Request,
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    company = await _first_company()
    items = await svc.list_active(session, company.id)
    return templates.TemplateResponse(
        request,
        "distributions/list.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "distributions": items,
        },
    )


@router.get("/distributions/new", response_class=HTMLResponse)
async def distributions_new_form(
    request: Request,
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    company = await _first_company()
    accounts = await _all_accounts(session, company.id)
    this_year = date.today().year
    return templates.TemplateResponse(
        request,
        "distributions/form.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "accounts": accounts,
            "financial_year": this_year,
            "distribution_date": f"{this_year}-06-30",
            "error": None,
        },
    )


@router.post("/distributions/new", response_model=None)
async def distributions_create(
    request: Request,
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse | HTMLResponse:
    company = await _first_company()
    form = dict(await request.form())

    try:
        financial_year = int(str(form.get("financial_year", "")))
        distribution_date = date.fromisoformat(str(form.get("distribution_date", "")))
        total_amount = Decimal(str(form.get("total_amount", "")))
        notes = str(form.get("notes", "")).strip() or None
    except (ValueError, InvalidOperation) as exc:
        accounts = await _all_accounts(session, company.id)
        return templates.TemplateResponse(
            request,
            "distributions/form.html",
            {
                "edition": settings.edition,
                "company_name": company.name,
                "accounts": accounts,
                "financial_year": form.get("financial_year", ""),
                "distribution_date": form.get("distribution_date", ""),
                "error": f"Invalid input: {exc}",
            },
        )

    # Parse dynamic beneficiary rows: name_1, pct_1, amount_1, account_id_1 ...
    entitlements: list[dict] = []
    i = 1
    while f"name_{i}" in form:
        name = str(form.get(f"name_{i}", "")).strip()
        if name:
            try:
                pct = Decimal(str(form.get(f"pct_{i}", "0")))
                amt = Decimal(str(form.get(f"amount_{i}", "0")))
            except InvalidOperation as exc:
                accounts = await _all_accounts(session, company.id)
                return templates.TemplateResponse(
                    request,
                    "distributions/form.html",
                    {
                        "edition": settings.edition,
                        "company_name": company.name,
                        "accounts": accounts,
                        "financial_year": financial_year,
                        "distribution_date": str(distribution_date),
                        "error": f"Invalid amount/percentage for row {i}: {exc}",
                    },
                )
            acct_raw = str(form.get(f"account_id_{i}", "")).strip()
            entitlements.append(
                {
                    "beneficiary_name": name,
                    "percentage": pct,
                    "amount": amt,
                    "account_id": acct_raw if acct_raw else None,
                    "notes": str(form.get(f"notes_{i}", "")).strip() or None,
                }
            )
        i += 1

    try:
        dist = await svc.create(
            session,
            company.id,
            financial_year=financial_year,
            distribution_date=distribution_date,
            total_amount=total_amount,
            notes=notes,
            entitlements=entitlements,
        )
    except svc.DistributionError as exc:
        await session.rollback()
        accounts = await _all_accounts(session, company.id)
        return templates.TemplateResponse(
            request,
            "distributions/form.html",
            {
                "edition": settings.edition,
                "company_name": company.name,
                "accounts": accounts,
                "financial_year": financial_year,
                "distribution_date": str(distribution_date),
                "error": str(exc),
            },
        )

    return RedirectResponse(f"/distributions/{dist.id}", status_code=303)


@router.get("/distributions/{distribution_id}", response_class=HTMLResponse)
async def distributions_detail(
    request: Request,
    distribution_id: uuid.UUID,
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    company = await _first_company()
    dist = await svc.get(session, distribution_id)
    if dist is None or dist.company_id != company.id:
        raise HTTPException(404, "Distribution not found")
    accounts = await _all_accounts(session, company.id)
    accounts_by_id = {a.id: a for a in accounts}
    equity_accounts = [a for a in accounts if a.account_type in _EQUITY_TYPES]
    income_accounts = [
        a for a in accounts
        if a.account_type in {AccountType.INCOME, AccountType.OTHER_INCOME}
    ]
    return templates.TemplateResponse(
        request,
        "distributions/detail.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "dist": dist,
            "accounts_by_id": accounts_by_id,
            "equity_accounts": equity_accounts,
            "income_accounts": income_accounts,
            "error": None,
        },
    )


@router.post("/distributions/{distribution_id}/minute", response_model=None)
async def distributions_minute(
    distribution_id: uuid.UUID,
    minuted_date: str = Form(...),
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse:
    try:
        md = date.fromisoformat(minuted_date)
    except ValueError as exc:
        raise HTTPException(400, f"Invalid date: {exc}") from exc
    try:
        await svc.minute(session, distribution_id, minuted_date=md)
    except svc.DistributionError as exc:
        raise HTTPException(422, str(exc)) from exc
    return RedirectResponse(f"/distributions/{distribution_id}", status_code=303)


@router.post("/distributions/{distribution_id}/post", response_model=None)
async def distributions_post_je(
    request: Request,
    distribution_id: uuid.UUID,
    income_account_id: str = Form(...),
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse | HTMLResponse:
    company = await _first_company()
    try:
        acct_id = uuid.UUID(income_account_id)
    except ValueError as exc:
        raise HTTPException(400, f"Invalid account ID: {exc}") from exc
    try:
        dist = await svc.post_journal_entry(
            session,
            distribution_id,
            income_account_id=acct_id,
        )
    except (svc.DistributionError, Exception) as exc:
        return RedirectResponse(
            f"/distributions/{distribution_id}?error={exc}",
            status_code=303,
        )
    return RedirectResponse(f"/journal/{dist.journal_entry_id}", status_code=303)


@router.post("/distributions/{distribution_id}/delete", response_model=None)
async def distributions_delete(
    distribution_id: uuid.UUID,
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse:
    try:
        await svc.delete(session, distribution_id)
    except svc.DistributionError as exc:
        raise HTTPException(422, str(exc)) from exc
    return RedirectResponse("/distributions", status_code=303)
