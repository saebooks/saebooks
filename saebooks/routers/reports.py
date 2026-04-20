"""Report routes — trial balance, P&L, balance sheet, aged debtors."""
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.services import bas as bas_svc
from saebooks.services import gst as gst_svc
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


@router.get("/bas", response_class=HTMLResponse)
async def bas_report(
    request: Request,
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
) -> HTMLResponse:
    company = await _first_company()
    fd = _parse_date(from_date)
    td = _parse_date(to_date)
    async with AsyncSessionLocal() as session:
        report = await bas_svc.bas_report(session, company.id, from_date=fd, to_date=td)
    return templates.TemplateResponse(
        request,
        "reports/bas.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "report": report,
            "from_date": from_date or "",
            "to_date": to_date or "",
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


@router.get("/aged-ar", response_class=HTMLResponse)
async def aged_ar_report(
    request: Request,
    as_at: str | None = Query(None),
    format: str = Query("html"),
) -> Response:
    company = await _first_company()
    cutoff = _parse_date(as_at) or date.today()
    async with AsyncSessionLocal() as session:
        report = await svc.aged_ar(session, company.id, as_at=cutoff)

    if format == "csv":
        csv_text = svc.aged_ar_csv(report)
        filename = f"aged-ar-{cutoff.isoformat()}.csv"
        return Response(
            content=csv_text,
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    return templates.TemplateResponse(
        request,
        "reports/aged_ar.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "report": report,
            "bucket_keys": svc.BUCKET_KEYS,
            "bucket_labels": svc.BUCKET_LABELS,
            "as_at": cutoff.isoformat(),
        },
    )


@router.post("/bas/settle", response_model=None)
async def bas_settle(request: Request) -> RedirectResponse:
    """Create a BAS settlement journal entry."""
    company = await _first_company()
    form = await request.form()
    from_date = _parse_date(str(form.get("from", "")))
    to_date = _parse_date(str(form.get("to", "")))
    settlement_date_raw = str(form.get("settlement_date", ""))
    settlement_date = _parse_date(settlement_date_raw)
    if not settlement_date:
        from datetime import date as date_cls
        settlement_date = date_cls.today()

    async with AsyncSessionLocal() as session:
        entry = await gst_svc.settle_bas(
            session,
            company.id,
            settlement_date=settlement_date,
            from_date=from_date,
            to_date=to_date,
        )

    if entry:
        return RedirectResponse(f"/journal/{entry.id}", status_code=303)

    # Nothing to settle — redirect back to BAS report
    params = ""
    if from_date:
        params += f"from={from_date}&"
    if to_date:
        params += f"to={to_date}&"
    return RedirectResponse(f"/reports/bas?{params}settled=empty", status_code=303)
