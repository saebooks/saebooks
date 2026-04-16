import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.account_range import AccountRange
from saebooks.models.company import Company
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine
from saebooks.models.tax_code import TaxCode
from saebooks.services import accounts as svc

_CREDIT_NORMAL = {AccountType.LIABILITY, AccountType.EQUITY, AccountType.INCOME, AccountType.OTHER_INCOME}

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

ACCOUNT_TYPE_CHOICES = [(t.value, t.value.replace("_", " ").title()) for t in AccountType]


async def _first_company() -> Company:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
        )
        company = result.scalars().first()
        if company is None:
            raise HTTPException(500, "No active company")
        return company


def _build_hierarchy(
    accounts: list[Account],
    ranges: list[AccountRange],
    balances: dict[uuid.UUID, Decimal] | None = None,
) -> list[dict[str, object]]:
    """Group accounts by matched range, with indent levels and balances.

    Accounts that don't match any range go into an "Unranged" group.
    """
    if balances is None:
        balances = {}

    # Build groups keyed by range prefix
    groups_by_prefix: dict[str, list[Account]] = {}
    unranged: list[Account] = []

    for a in accounts:
        parsed = svc.parse_code(a.code, ranges)
        if parsed:
            groups_by_prefix.setdefault(parsed.prefix, []).append(a)
        else:
            unranged.append(a)

    groups: list[dict[str, object]] = []

    for rng in sorted(ranges, key=lambda r: r.sort_order):
        accts = groups_by_prefix.get(rng.prefix, [])
        if not accts:
            continue

        group_total = Decimal("0")
        rows = []
        for a in accts:
            parsed = svc.parse_code(a.code, ranges)
            depth = parsed.depth if parsed else 0
            anomaly = svc.check_code_anomaly(a.code, a.account_type, ranges)
            bal = balances.get(a.id, Decimal("0"))
            group_total += bal
            protected = a.code in _PROTECTED_CODES or a.system_managed
            rows.append({
                "account": a,
                "indent": depth,
                "anomaly": anomaly,
                "bustard": parsed.bustard if parsed else "",
                "balance": bal,
                "protected": protected,
            })

        groups.append({
            "prefix": rng.prefix,
            "label": rng.label,
            "count": len(accts),
            "rows": rows,
            "allowed_types": rng.account_types,
            "total": group_total,
        })

    # Unranged accounts (legacy/seed data that doesn't match any range)
    if unranged:
        group_total = Decimal("0")
        rows = []
        for a in unranged:
            bal = balances.get(a.id, Decimal("0"))
            group_total += bal
            rows.append({
                "account": a,
                "indent": 0,
                "anomaly": f"Code '{a.code}' doesn't match any defined range",
                "bustard": "",
                "balance": bal,
                "protected": a.code in _PROTECTED_CODES or a.system_managed,
            })
        groups.append({
            "prefix": "?",
            "label": "Unranged",
            "count": len(unranged),
            "rows": rows,
            "allowed_types": [],
            "total": group_total,
        })

    return groups


# Accounts that are protected from casual editing — require confirmation
# These are structural accounts that affect the integrity of the ledger
_PROTECTED_CODES = {
    "3-8000",  # Retained Earnings
    "3-9000",  # Current Year Earnings
    "3-9999",  # Historical Balancing
}


async def _account_balances(
    session: AsyncSession,
    company_id: uuid.UUID,
) -> dict[uuid.UUID, Decimal]:
    """Get current balance for every account (from POSTED entries only).

    Returns a dict of account_id → signed balance.
    Credit-normal accounts have positive balance when in credit.
    Debit-normal accounts have positive balance when in debit.
    """
    stmt = (
        select(
            JournalLine.account_id,
            func.coalesce(func.sum(JournalLine.debit), Decimal("0")).label("tot_dr"),
            func.coalesce(func.sum(JournalLine.credit), Decimal("0")).label("tot_cr"),
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(
            JournalEntry.company_id == company_id,
            JournalEntry.status == EntryStatus.POSTED,
        )
        .group_by(JournalLine.account_id)
    )
    rows = (await session.execute(stmt)).all()

    # We need account types to compute signed balances
    acct_ids = [r.account_id for r in rows]
    if not acct_ids:
        return {}

    type_stmt = select(Account.id, Account.account_type).where(Account.id.in_(acct_ids))
    type_rows = (await session.execute(type_stmt)).all()
    type_map = {r.id: r.account_type for r in type_rows}

    balances: dict[uuid.UUID, Decimal] = {}
    for r in rows:
        at = type_map.get(r.account_id)
        if at and at in _CREDIT_NORMAL:
            balances[r.account_id] = r.tot_cr - r.tot_dr
        else:
            balances[r.account_id] = r.tot_dr - r.tot_cr
    return balances


async def _tax_codes(company_id: uuid.UUID) -> list[TaxCode]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(TaxCode)
            .where(TaxCode.company_id == company_id, TaxCode.archived_at.is_(None))
            .order_by(TaxCode.code)
        )
        return list(result.scalars().all())


@router.get("/accounts", response_class=HTMLResponse)
async def accounts_list(request: Request) -> HTMLResponse:
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        accounts = await svc.list_active(session, company.id)
        ranges = await svc.get_ranges(session, company.id)

        # Auto-seed default ranges if none exist yet
        if not ranges:
            await svc.seed_default_ranges(session, company.id)
            ranges = await svc.get_ranges(session, company.id)

        balances = await _account_balances(session, company.id)

    tax_codes = await _tax_codes(company.id)
    groups = _build_hierarchy(accounts, ranges, balances)

    return templates.TemplateResponse(
        request,
        "accounts/list.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "total": len(accounts),
            "groups": groups,
            "ranges": ranges,
            "account_types": ACCOUNT_TYPE_CHOICES,
            "tax_codes": tax_codes,
        },
    )


@router.post("/accounts", response_model=None)
async def accounts_create(
    request: Request,
    code: str = Form(...),
    name: str = Form(...),
    account_type: str = Form(...),
    reconcile: bool = Form(False),
    is_header: bool = Form(False),
    tax_code_default: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    company = await _first_company()
    try:
        async with AsyncSessionLocal() as session:
            await svc.create(
                session,
                company.id,
                code=code,
                name=name,
                account_type=AccountType(account_type),
                reconcile=reconcile,
                is_header=is_header,
                tax_code_default=tax_code_default or None,
            )
    except ValueError as exc:
        async with AsyncSessionLocal() as session:
            accounts = await svc.list_active(session, company.id)
            ranges = await svc.get_ranges(session, company.id)
            bal = await _account_balances(session, company.id)
        groups = _build_hierarchy(accounts, ranges, bal)
        return templates.TemplateResponse(
            request,
            "accounts/list.html",
            {
                "edition": settings.edition,
                "company_name": company.name,
                "total": len(accounts),
                "groups": groups,
                "ranges": ranges,
                "account_types": ACCOUNT_TYPE_CHOICES,
                "error": str(exc),
                "form_code": code,
                "form_name": name,
                "form_type": account_type,
            },
            status_code=422,
        )
    return RedirectResponse("/accounts", status_code=303)


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


@router.get("/accounts/{account_id}", response_class=HTMLResponse)
async def accounts_detail(
    request: Request,
    account_id: UUID,
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
) -> HTMLResponse:
    parsed_from = _parse_date(from_date)
    parsed_to = _parse_date(to_date)

    async with AsyncSessionLocal() as session:
        account = await svc.get(session, account_id)
        if account is None:
            raise HTTPException(404, "Account not found")
        company = await session.get(Company, account.company_id)

        credit_normal = account.account_type in _CREDIT_NORMAL

        # Opening balance: sum of all posted lines before from_date
        opening_balance = Decimal("0")
        if parsed_from:
            ob_stmt = (
                select(
                    func.coalesce(func.sum(JournalLine.debit), Decimal("0")).label("tot_dr"),
                    func.coalesce(func.sum(JournalLine.credit), Decimal("0")).label("tot_cr"),
                )
                .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
                .where(
                    JournalLine.account_id == account_id,
                    JournalEntry.status == EntryStatus.POSTED,
                    JournalEntry.entry_date < parsed_from,
                )
            )
            ob_row = (await session.execute(ob_stmt)).one()
            if credit_normal:
                opening_balance = ob_row.tot_cr - ob_row.tot_dr
            else:
                opening_balance = ob_row.tot_dr - ob_row.tot_cr

        # Main query: all posted lines for this account within date range
        stmt = (
            select(JournalLine, JournalEntry)
            .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
            .where(
                JournalLine.account_id == account_id,
                JournalEntry.status == EntryStatus.POSTED,
            )
            .order_by(JournalEntry.entry_date.asc(), JournalLine.line_no.asc())
        )
        if parsed_from:
            stmt = stmt.where(JournalEntry.entry_date >= parsed_from)
        if parsed_to:
            stmt = stmt.where(JournalEntry.entry_date <= parsed_to)

        rows = (await session.execute(stmt)).all()

    # Build display rows with running balance
    running = opening_balance
    total_debit = Decimal("0")
    total_credit = Decimal("0")
    lines = []
    for jl, je in rows:
        total_debit += jl.debit
        total_credit += jl.credit
        if credit_normal:
            running += jl.credit - jl.debit
        else:
            running += jl.debit - jl.credit
        lines.append({
            "entry_id": je.id,
            "entry_date": je.entry_date,
            "ref": je.ref,
            "description": jl.description or je.description or "",
            "debit": jl.debit,
            "credit": jl.credit,
            "balance": running,
        })

    closing_balance = running

    return templates.TemplateResponse(
        request,
        "accounts/detail.html",
        {
            "edition": settings.edition,
            "company_name": company.name if company else "",
            "account": account,
            "credit_normal": credit_normal,
            "lines": lines,
            "opening_balance": opening_balance,
            "total_debit": total_debit,
            "total_credit": total_credit,
            "closing_balance": closing_balance,
            "from_date": from_date or "",
            "to_date": to_date or "",
            "has_from": parsed_from is not None,
        },
    )


@router.get("/accounts/{account_id}/edit", response_class=HTMLResponse)
async def accounts_edit(request: Request, account_id: UUID) -> HTMLResponse:
    async with AsyncSessionLocal() as session:
        account = await svc.get(session, account_id)
        if account is None:
            raise HTTPException(404, "Account not found")
        company = await session.get(Company, account.company_id)
        ranges = await svc.get_ranges(session, account.company_id)
    protected = account.code in _PROTECTED_CODES or account.system_managed
    return templates.TemplateResponse(
        request,
        "accounts/edit.html",
        {
            "edition": settings.edition,
            "company_name": company.name if company else "",
            "account": account,
            "account_types": ACCOUNT_TYPE_CHOICES,
            "ranges": ranges,
            "protected": protected,
        },
    )


@router.post("/accounts/{account_id}", response_model=None)
async def accounts_update(
    request: Request,
    account_id: UUID,
    code: str = Form(...),
    name: str = Form(...),
    account_type: str = Form(...),
    reconcile: bool = Form(False),
    is_header: bool = Form(False),
    tax_code_default: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    try:
        async with AsyncSessionLocal() as session:
            await svc.update(
                session,
                account_id,
                code=code,
                name=name,
                account_type=AccountType(account_type),
                reconcile=reconcile,
                is_header=is_header,
                tax_code_default=tax_code_default,
                performed_by="web",
            )
    except ValueError as exc:
        async with AsyncSessionLocal() as session:
            account = await svc.get(session, account_id)
            if account is None:
                raise HTTPException(404, "Account not found") from exc
            company = await session.get(Company, account.company_id)
            ranges = await svc.get_ranges(session, account.company_id)
        return templates.TemplateResponse(
            request,
            "accounts/edit.html",
            {
                "edition": settings.edition,
                "company_name": company.name if company else "",
                "account": account,
                "account_types": ACCOUNT_TYPE_CHOICES,
                "ranges": ranges,
                "error": str(exc),
            },
            status_code=422,
        )
    return RedirectResponse("/accounts", status_code=303)


@router.post("/accounts/{account_id}/archive")
async def accounts_archive(account_id: UUID) -> RedirectResponse:
    async with AsyncSessionLocal() as session:
        await svc.archive(session, account_id)
    return RedirectResponse("/accounts", status_code=303)


@router.get("/accounts/{account_id}/delete", response_class=HTMLResponse)
async def accounts_delete_check(request: Request, account_id: UUID) -> HTMLResponse:
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        deps = await svc.check_dependencies(session, account_id)
        all_accounts = await svc.list_active(session, company.id)
        candidates = [
            a for a in all_accounts
            if a.id != account_id and a.account_type == deps.account.account_type
        ]

    return templates.TemplateResponse(
        request,
        "accounts/delete.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "deps": deps,
            "candidates": candidates,
        },
    )


@router.post("/accounts/{account_id}/migrate", response_model=None)
async def accounts_migrate(
    request: Request,
    account_id: UUID,
) -> RedirectResponse | HTMLResponse:
    form = await request.form()
    target_raw = str(form.get("target_id", ""))
    if not target_raw:
        return RedirectResponse(f"/accounts/{account_id}/delete?error=no_target", status_code=303)

    target_id = UUID(target_raw)
    async with AsyncSessionLocal() as session:
        counts = await svc.migrate_account(
            session, account_id, target_id, performed_by="web"
        )

    total = sum(counts.values())
    return RedirectResponse(
        f"/accounts/{account_id}/delete?migrated={total}",
        status_code=303,
    )


@router.post("/accounts/{account_id}/delete", response_model=None)
async def accounts_delete(
    request: Request,
    account_id: UUID,
) -> RedirectResponse | HTMLResponse:
    try:
        async with AsyncSessionLocal() as session:
            await svc.delete_account(session, account_id, performed_by="web")
    except Exception as exc:
        return RedirectResponse(
            f"/accounts/{account_id}/delete?error={exc}",
            status_code=303,
        )
    return RedirectResponse("/accounts?deleted=1", status_code=303)


# ---------------------------------------------------------------------------
# Inline edits (HTMX) — tax code and reconcile from the list page
# ---------------------------------------------------------------------------


@router.patch("/accounts/{account_id}/tax-code", response_class=HTMLResponse)
async def accounts_set_tax_code(
    request: Request, account_id: UUID
) -> HTMLResponse:
    """HTMX inline: set the default tax code for an account."""
    form = await request.form()
    new_code = str(form.get("tax_code_default", "")).strip()
    async with AsyncSessionLocal() as session:
        await svc.update(
            session, account_id,
            tax_code_default=new_code or None,
            skip_validation=True,
            performed_by="web",
        )
        account = await svc.get(session, account_id)
    if account is None:
        raise HTTPException(404)
    # Return just the cell content for HTMX swap
    display = account.tax_code_default or ""
    return HTMLResponse(
        f'<span class="tc-display" hx-get="/accounts/{account_id}/tax-code/edit"'
        f' hx-swap="outerHTML" hx-trigger="click" title="Click to change">'
        f'{display or "<em class=dim>none</em>"}</span>'
    )


@router.get("/accounts/{account_id}/tax-code/edit", response_class=HTMLResponse)
async def accounts_edit_tax_code_inline(
    request: Request, account_id: UUID
) -> HTMLResponse:
    """HTMX inline: render the tax code dropdown for an account."""
    company = await _first_company()
    tax_codes = await _tax_codes(company.id)
    async with AsyncSessionLocal() as session:
        account = await svc.get(session, account_id)
    if account is None:
        raise HTTPException(404)
    current = account.tax_code_default or ""
    options = ['<option value="">— none —</option>']
    for tc in tax_codes:
        sel = " selected" if tc.code == current else ""
        options.append(f'<option value="{tc.code}"{sel}>{tc.code} — {tc.name}</option>')
    opts_html = "\n".join(options)
    return HTMLResponse(
        f'<form hx-patch="/accounts/{account_id}/tax-code" hx-swap="outerHTML"'
        f' hx-trigger="change" style="display:inline">'
        f'<select name="tax_code_default" class="inline-tc" onblur="htmx.trigger(this.form,\'change\')">'
        f'{opts_html}</select></form>'
    )


# ---------------------------------------------------------------------------
# Bulk edit
# ---------------------------------------------------------------------------


@router.post("/accounts/bulk", response_model=None)
async def accounts_bulk_update(request: Request) -> RedirectResponse:
    """Apply a bulk action to selected accounts."""
    form = dict(await request.form())

    # Collect selected IDs
    selected: list[UUID] = []
    for key, val in form.items():
        if key.startswith("sel_") and val == "on":
            selected.append(UUID(key[4:]))

    if not selected:
        return RedirectResponse("/accounts?error=no_selection", status_code=303)

    action = str(form.get("bulk_action", ""))
    bulk_val = str(form.get("bulk_value", "")).strip()

    async with AsyncSessionLocal() as session:
        if action == "tax_code":
            for aid in selected:
                await svc.update(
                    session, aid,
                    tax_code_default=bulk_val or None,
                    skip_validation=True,
                    performed_by="web-bulk",
                )
        elif action == "reconcile":
            for aid in selected:
                await svc.update(
                    session, aid,
                    reconcile=bulk_val == "true",
                    skip_validation=True,
                    performed_by="web-bulk",
                )

    return RedirectResponse("/accounts", status_code=303)
