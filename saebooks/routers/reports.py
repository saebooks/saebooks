"""Report routes — trial balance, P&L, balance sheet, aged debtors."""
import uuid
from collections.abc import Sequence
from datetime import date

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.services import assets_reports as assets_reports_svc
from saebooks.services import bas as bas_svc
from saebooks.services import gst as gst_svc
from saebooks.services import period_close as period_close_svc
from saebooks.services import reports as svc
from saebooks.services import tpar as tpar_svc
from saebooks.services import trust_reports as trust_svc
from saebooks.services.fx import rates as fx_rates_svc
from saebooks.services.fx import reval as fx_reval_svc
from saebooks.web import templates
from saebooks.services import active_company as active_svc
from saebooks.api.v1.auth import resolve_tenant_id

router = APIRouter(prefix="/reports")


async def _first_company() -> Company:
    return await active_svc.first_company_compat()


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


@router.get("/aged-ap", response_class=HTMLResponse)
async def aged_ap_report(
    request: Request,
    as_at: str | None = Query(None),
    format: str = Query("html"),
) -> Response:
    company = await _first_company()
    cutoff = _parse_date(as_at) or date.today()
    async with AsyncSessionLocal() as session:
        report = await svc.aged_ap(session, company.id, as_at=cutoff)

    if format == "csv":
        csv_text = svc.aged_ap_csv(report)
        filename = f"aged-ap-{cutoff.isoformat()}.csv"
        return Response(
            content=csv_text,
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    return templates.TemplateResponse(
        request,
        "reports/aged_ap.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "report": report,
            "bucket_keys": svc.BUCKET_KEYS,
            "bucket_labels": svc.BUCKET_LABELS,
            "as_at": cutoff.isoformat(),
        },
    )


@router.get("/close-year", response_class=HTMLResponse)
async def close_year_form(
    request: Request,
    through: str | None = Query(None),
    retained_earnings_account_id: str | None = Query(None),
) -> HTMLResponse:
    """Preview the year-end close. No state change here.

    Defaults:
    - ``through`` — last financial-year-end; for AU that's the most
      recent 30 June. Fallback to today when a company's FY dates
      aren't configured.
    - ``retained_earnings_account_id`` — first EQUITY account with
      "retained earnings" in its name (the AU seed ships one as
      ``3-8000 Retained Earnings``).
    """
    company = await _first_company()
    through_date = _parse_date(through) or _default_fy_end(company)

    async with AsyncSessionLocal() as session:
        equity_accounts = (
            await session.execute(
                select(Account)
                .where(
                    Account.company_id == company.id,
                    Account.account_type == AccountType.EQUITY,
                    Account.archived_at.is_(None),
                )
                .order_by(Account.code)
            )
        ).scalars().all()

        retained_id = (
            _uuid_or_none(retained_earnings_account_id)
            or _default_retained_earnings(equity_accounts)
        )

        preview = None
        if retained_id is not None:
            preview = await period_close_svc.preview_close(
                session,
                company.id,
                through_date=through_date,
                retained_earnings_account_id=retained_id,
            )

    return templates.TemplateResponse(
        request,
        "reports/close_year.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "through": through_date.isoformat(),
            "equity_accounts": equity_accounts,
            "retained_earnings_id": str(retained_id) if retained_id else "",
            "preview": preview,
        },
    )


@router.post("/close-year", response_model=None)
async def close_year_submit(request: Request) -> RedirectResponse:
    """Post the year-end close journal, then lock the period."""
    company = await _first_company()
    form = await request.form()
    through_date = _parse_date(str(form.get("through", "")))
    retained_id_raw = str(form.get("retained_earnings_account_id", ""))
    retained_id = _uuid_or_none(retained_id_raw)
    if through_date is None or retained_id is None:
        raise HTTPException(
            400, "Both 'through' date and retained-earnings account are required"
        )
    posted_by = request.headers.get("remote-user") or None

    tenant_id = resolve_tenant_id(request)

    async with AsyncSessionLocal() as session:
        entry = await period_close_svc.close_year(
            session,
            company.id,
            tenant_id=tenant_id,
            through_date=through_date,
            retained_earnings_account_id=retained_id,
            posted_by=posted_by,
        )

    if entry is None:
        return RedirectResponse(
            f"/reports/close-year?through={through_date.isoformat()}&closed=empty",
            status_code=303,
        )
    return RedirectResponse(f"/journal/{entry.id}", status_code=303)


def _default_fy_end(company: Company) -> date:
    """Return the most recent FY-end date for the company.

    AU default: 30 June. If today is 2026-04-21 we return 2025-06-30.
    """
    today = date.today()
    fy_month = company.fin_year_start_month or 7
    # FY ends the day before fy_month starts. E.g. fy_month=7 → 30 June.
    end_month = 12 if fy_month == 1 else fy_month - 1
    import calendar
    end_day = calendar.monthrange(today.year, end_month)[1]
    candidate = date(today.year, end_month, end_day)
    if candidate <= today:
        return candidate
    return date(today.year - 1, end_month, end_day)


def _default_retained_earnings(accounts: Sequence[Account]) -> uuid.UUID | None:
    """Pick the first equity account that looks like retained earnings."""
    for a in accounts:
        if "retained earnings" in a.name.lower():
            return a.id
    # Fallback: first equity account at all
    return accounts[0].id if accounts else None


def _uuid_or_none(raw: str | None) -> uuid.UUID | None:
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


@router.get("/pl-by-segment", response_class=HTMLResponse)
async def pl_by_segment_report(
    request: Request,
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    segment: str = Query("project"),
) -> HTMLResponse:
    """P&L grouped by segment (project for v1)."""
    company = await _first_company()
    fd = _parse_date(from_date)
    td = _parse_date(to_date)
    async with AsyncSessionLocal() as session:
        segments = await svc.pl_by_segment(
            session, company.id,
            from_date=fd, to_date=td, segment=segment,
        )
    return templates.TemplateResponse(
        request,
        "reports/pl_by_segment.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "segments": segments,
            "segment": segment,
            "from_date": from_date or "",
            "to_date": to_date or "",
        },
    )


@router.get("/budget-vs-actual", response_class=HTMLResponse)
async def budget_vs_actual_report(
    request: Request,
    year: int | None = Query(None),
) -> HTMLResponse:
    """Budget vs actual per P&L account for a calendar year."""
    company = await _first_company()
    year_val = year or date.today().year
    async with AsyncSessionLocal() as session:
        report = await svc.budget_vs_actual(session, company.id, year=year_val)
    month_labels = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ]
    years = list(range(year_val - 2, year_val + 3))
    return templates.TemplateResponse(
        request,
        "reports/budget_vs_actual.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "report": report,
            "year": year_val,
            "years": years,
            "month_labels": month_labels,
        },
    )


@router.get("/cashflow-forecast", response_class=HTMLResponse)
async def cashflow_forecast_report(
    request: Request,
    horizon: int = Query(90),
    as_of: str | None = Query(None),
) -> HTMLResponse:
    """Rolling cash-flow forecast: open AR + AP + recurring, weekly roll-up."""
    company = await _first_company()
    as_of_date = _parse_date(as_of) or date.today()
    horizon_days = max(7, min(365, horizon))
    async with AsyncSessionLocal() as session:
        report = await svc.cashflow_forecast(
            session, company.id,
            horizon_days=horizon_days,
            as_of=as_of_date,
        )
    return templates.TemplateResponse(
        request,
        "reports/cashflow_forecast.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "report": report,
            "horizon": horizon_days,
            "as_of": as_of_date.isoformat(),
        },
    )


@router.get("/depreciation-schedule", response_class=HTMLResponse)
async def depreciation_schedule_report(
    request: Request,
    as_at: str | None = Query(None),
    include_disposed: bool = Query(False),
    format: str = Query("html"),
) -> Response:
    """Fixed-asset depreciation schedule with book vs tax overlay.

    Reporting-only — tax cumulative is computed on-demand from the
    asset's ``tax_model_id`` (or the book model when NULL). No GL
    side effects.
    """
    company = await _first_company()
    cutoff = _parse_date(as_at) or date.today()
    async with AsyncSessionLocal() as session:
        schedule = await assets_reports_svc.depreciation_schedule(
            session,
            company.id,
            as_at=cutoff,
            include_disposed=include_disposed,
        )

    if format == "csv":
        csv_text = assets_reports_svc.depreciation_schedule_csv(schedule)
        filename = f"depreciation-schedule-{cutoff.isoformat()}.csv"
        return Response(
            content=csv_text,
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    return templates.TemplateResponse(
        request,
        "reports/depreciation_schedule.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "schedule": schedule,
            "as_at": cutoff.isoformat(),
            "include_disposed": include_disposed,
        },
    )


@router.get("/fx-revalue", response_class=HTMLResponse)
async def fx_revalue_form(
    request: Request,
    through: str | None = Query(None),
) -> HTMLResponse:
    """Preview open foreign-currency AR/AP + the revalued base position.

    No state change — the form posts to ``/reports/fx-revalue`` to
    actually run ``revalue_company``. An ``FxRateError`` (no rate, no
    registered fetcher) is caught and surfaced inline so the user
    understands they need to seed a rate manually.
    """
    company = await _first_company()
    through_date = _parse_date(through) or date.today()

    preview: list[fx_reval_svc.CurrencyReval] = []
    rate_error: str | None = None
    async with AsyncSessionLocal() as session:
        try:
            preview = await fx_reval_svc.preview_company(
                session,
                company_id=company.id,
                through_date=through_date,
            )
        except fx_rates_svc.FxRateError as exc:
            rate_error = str(exc)

    return templates.TemplateResponse(
        request,
        "reports/fx_revalue.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "through": through_date.isoformat(),
            "preview": preview,
            "rate_error": rate_error,
        },
    )


@router.post("/fx-revalue", response_model=None)
async def fx_revalue_submit(request: Request) -> RedirectResponse:
    """Post the adjusting + reversing pair per foreign currency."""
    company = await _first_company()
    form = await request.form()
    through_date = _parse_date(str(form.get("through", "")))
    if through_date is None:
        raise HTTPException(400, "'through' date is required")
    posted_by = request.headers.get("remote-user") or None

    tenant_id = resolve_tenant_id(request)

    try:
        async with AsyncSessionLocal() as session:
            result = await fx_reval_svc.revalue_company(
                session,
                company_id=company.id,
                tenant_id=tenant_id,
                through_date=through_date,
                posted_by=posted_by,
            )
    except (fx_rates_svc.FxRateError, fx_reval_svc.FxRevalError) as exc:
        return RedirectResponse(
            f"/reports/fx-revalue?through={through_date.isoformat()}"
            f"&error={exc.__class__.__name__}",
            status_code=303,
        )

    return RedirectResponse(
        f"/reports/fx-revalue?through={through_date.isoformat()}"
        f"&posted={result.posted_count}",
        status_code=303,
    )


@router.get("/trust-cashbook", response_class=HTMLResponse)
async def trust_cashbook_report(
    request: Request,
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
) -> HTMLResponse:
    """Trust Account Receipts & Payments Cash Book (NSW PSAA 2002 s.105)."""
    company = await _first_company()
    fd = _parse_date(from_date)
    td = _parse_date(to_date)
    async with AsyncSessionLocal() as session:
        reports = await trust_svc.trust_cashbook(session, company.id, from_date=fd, to_date=td)
    return templates.TemplateResponse(
        request,
        "reports/trust_cashbook.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "reports": reports,
            "from_date": from_date or "",
            "to_date": to_date or "",
        },
    )


@router.get("/trust-balances", response_class=HTMLResponse)
async def trust_balances_report(
    request: Request,
    as_of: str | None = Query(None),
) -> HTMLResponse:
    """Unreconciled Trust Balances — liability to beneficiaries (NSW PSAA 2002)."""
    company = await _first_company()
    as_of_date = _parse_date(as_of)
    async with AsyncSessionLocal() as session:
        report = await trust_svc.unreconciled_trust_balances(
            session, company.id, as_of=as_of_date
        )
    return templates.TemplateResponse(
        request,
        "reports/trust_balances.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "report": report,
            "as_of": as_of or "",
        },
    )


@router.get("/tpar", response_class=HTMLResponse)
async def tpar_report(
    request: Request,
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
) -> HTMLResponse:
    """Taxable Payments Annual Report — sub-contractor payments for ATO lodgement."""
    company = await _first_company()
    fd = _parse_date(from_date)
    td = _parse_date(to_date)
    async with AsyncSessionLocal() as session:
        report = await tpar_svc.tpar_report(session, company.id, from_date=fd, to_date=td)
    return templates.TemplateResponse(
        request,
        "reports/tpar.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "report": report,
            "from_date": from_date or "",
            "to_date": to_date or "",
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

    tenant_id = resolve_tenant_id(request)

    async with AsyncSessionLocal() as session:
        entry = await gst_svc.settle_bas(
            session,
            company.id,
            tenant_id=tenant_id,
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
