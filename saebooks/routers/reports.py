"""Report routes — trial balance, P&L, balance sheet."""
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.services import reports as svc

router = APIRouter(prefix="/reports")

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


async def _first_company() -> Company:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
        )
        company = result.scalars().first()
        if company is None:
            raise HTTPException(500, "No active company")
        return company


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    return date.fromisoformat(raw)


@router.get("", response_class=HTMLResponse)
async def reports_index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "reports/index.html",
        {"edition": settings.edition},
    )


@router.get("/trial-balance", response_class=HTMLResponse)
async def trial_balance(
    request: Request,
    as_of: str | None = Query(None),
) -> HTMLResponse:
    company = await _first_company()
    as_of_date = _parse_date(as_of)
    async with AsyncSessionLocal() as session:
        sections = await svc.trial_balance(session, company.id, as_of=as_of_date)
    total_dr = sum(s.total_debit for s in sections)
    total_cr = sum(s.total_credit for s in sections)
    return templates.TemplateResponse(
        request,
        "reports/trial_balance.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "sections": sections,
            "as_of": as_of or "",
            "total_debit": total_dr,
            "total_credit": total_cr,
        },
    )


@router.get("/profit-loss", response_class=HTMLResponse)
async def profit_loss(
    request: Request,
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
) -> HTMLResponse:
    company = await _first_company()
    fd = _parse_date(from_date)
    td = _parse_date(to_date)
    async with AsyncSessionLocal() as session:
        sections, net_profit = await svc.profit_and_loss(
            session, company.id, from_date=fd, to_date=td
        )
    return templates.TemplateResponse(
        request,
        "reports/profit_loss.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "sections": sections,
            "from_date": from_date or "",
            "to_date": to_date or "",
            "net_profit": net_profit,
        },
    )


@router.get("/balance-sheet", response_class=HTMLResponse)
async def balance_sheet_report(
    request: Request,
    as_of: str | None = Query(None),
) -> HTMLResponse:
    company = await _first_company()
    as_of_date = _parse_date(as_of)
    async with AsyncSessionLocal() as session:
        sections, net_assets = await svc.balance_sheet(
            session, company.id, as_of=as_of_date
        )
    return templates.TemplateResponse(
        request,
        "reports/balance_sheet.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "sections": sections,
            "as_of": as_of or "",
            "net_assets": net_assets,
        },
    )
