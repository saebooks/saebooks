"""Budget routes — per-account x per-month amount grid.

Budgets are a reporting overlay (see ``services/reports.py:budget_vs_actual``).
They never hit the GL. UX is "pick an account + year, edit 12 monthly
cells" — saved through ``services/budgets.py:bulk_upsert`` in one go.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import settings
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.routers.deps import get_web_session
from saebooks.services import active_company as active_svc
from saebooks.services import budgets as svc
from saebooks.web import templates

router = APIRouter(prefix="/budgets")


# Account types that are legitimate budget targets — P&L side plus
# capex asset accounts. Balance-sheet control accounts (AR/AP/Bank) are
# not useful to budget and are intentionally excluded.
_BUDGETABLE_TYPES = {
    AccountType.INCOME,
    AccountType.OTHER_INCOME,
    AccountType.EXPENSE,
    AccountType.COST_OF_SALES,
    AccountType.OTHER_EXPENSE,
}

MONTH_LABELS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


async def _first_company() -> Company:
    return await active_svc.first_company_compat()


async def _budgetable_accounts(
    session: AsyncSession, company_id: uuid.UUID
) -> list[Account]:
    result = await session.execute(
        select(Account)
        .where(
            Account.company_id == company_id,
            Account.archived_at.is_(None),
            Account.is_header.is_(False),
        )
        .order_by(Account.code)
    )
    return [
        a for a in result.scalars().all() if a.account_type in _BUDGETABLE_TYPES
    ]


def _default_year() -> int:
    return date.today().year


# ---------------------------------------------------------------------- #
# Index — summary + quick picker                                          #
# ---------------------------------------------------------------------- #


@router.get("", response_class=HTMLResponse)
async def budgets_index(
    request: Request,
    year: int = Query(default_factory=_default_year),
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    company = await _first_company()
    accounts = await _budgetable_accounts(session, company.id)
    rows = await svc.list_for_period(session, company.id, year=year)
    # Roll up: account_id -> total + per-month map
    by_account: dict[uuid.UUID, dict[str, Any]] = {}
    for row in rows:
        bucket = by_account.setdefault(
            row.account_id,
            {"total": Decimal("0"), "months": {}},
        )
        bucket["total"] += row.amount
        bucket["months"][row.month] = row.amount
    # Zip with accounts for stable display order
    account_summaries = []
    grand_total = Decimal("0")
    for acct in accounts:
        summary = by_account.get(acct.id, {"total": Decimal("0"), "months": {}})
        if summary["total"] != 0:
            account_summaries.append(
                {
                    "account": acct,
                    "total": summary["total"],
                    "months": summary["months"],
                }
            )
            grand_total += summary["total"]
    return templates.TemplateResponse(
        request,
        "budgets/index.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "year": year,
            "years": list(range(_default_year() - 2, _default_year() + 3)),
            "accounts": accounts,
            "account_summaries": account_summaries,
            "grand_total": grand_total,
            "month_labels": MONTH_LABELS,
        },
    )


# ---------------------------------------------------------------------- #
# Grid edit                                                               #
# ---------------------------------------------------------------------- #


@router.get("/edit", response_class=HTMLResponse)
async def budgets_edit(
    request: Request,
    account_id: uuid.UUID = Query(...),
    year: int = Query(default_factory=_default_year),
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    company = await _first_company()
    account = await session.get(Account, account_id)
    if account is None or account.company_id != company.id:
        raise HTTPException(404, "Account not found")
    rows = await svc.list_for_period(
        session, company.id, year=year, account_id=account_id
    )
    # Build month -> {amount, notes} pre-fill map.
    by_month = {r.month: r for r in rows}
    monthly = []
    for m in range(1, 13):
        row = by_month.get(m)
        monthly.append(
            {
                "month": m,
                "label": MONTH_LABELS[m - 1],
                "amount": row.amount if row else Decimal("0"),
                "notes": row.notes if row else "",
            }
        )
    return templates.TemplateResponse(
        request,
        "budgets/grid.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "account": account,
            "year": year,
            "monthly": monthly,
            "total": sum(
                (Decimal(str(m["amount"])) for m in monthly), Decimal("0")
            ),
            "error": None,
        },
    )


@router.post("/save", response_model=None)
async def budgets_save(
    request: Request,
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse | HTMLResponse:
    company = await _first_company()
    form = dict(await request.form())
    try:
        account_id = uuid.UUID(str(form["account_id"]))
        year = int(str(form["year"]))
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, "Missing or invalid account_id/year") from exc

    rows: list[dict[str, Any]] = []
    for m in range(1, 13):
        amt_raw = str(form.get(f"month_{m}_amount", "0")).strip() or "0"
        notes_raw = str(form.get(f"month_{m}_notes", "")).strip()
        try:
            amount = Decimal(amt_raw)
        except InvalidOperation as exc:
            raise HTTPException(
                400, f"Invalid amount for month {m}: {amt_raw!r}"
            ) from exc
        rows.append(
            {
                "account_id": account_id,
                "month": m,
                "amount": amount,
                "notes": notes_raw or None,
            }
        )

    await svc.bulk_upsert(session, company.id, year=year, rows=rows)

    return RedirectResponse(f"/budgets?year={year}", status_code=303)
