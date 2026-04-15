from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

GROUP_ORDER: list[tuple[AccountType, str]] = [
    (AccountType.ASSET, "Assets"),
    (AccountType.LIABILITY, "Liabilities"),
    (AccountType.EQUITY, "Equity"),
    (AccountType.INCOME, "Income"),
    (AccountType.OTHER_INCOME, "Other income"),
    (AccountType.COST_OF_SALES, "Cost of sales"),
    (AccountType.EXPENSE, "Expenses"),
    (AccountType.OTHER_EXPENSE, "Other expense"),
]


@router.get("/accounts", response_class=HTMLResponse)
async def accounts_list(request: Request) -> HTMLResponse:
    async with AsyncSessionLocal() as session:
        company_result = await session.execute(
            select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
        )
        company = company_result.scalars().first()
        company_name = company.name if company else "(no company)"

        stmt = select(Account).order_by(Account.code)
        if company is not None:
            stmt = stmt.where(Account.company_id == company.id)
        result = await session.execute(stmt)
        accounts = list(result.scalars().all())

    by_type: dict[AccountType, list[Account]] = {}
    for a in accounts:
        by_type.setdefault(a.account_type, []).append(a)

    groups = [
        {"label": label, "accounts": by_type[t]}
        for t, label in GROUP_ORDER
        if by_type.get(t)
    ]
    return templates.TemplateResponse(
        request,
        "accounts/list.html",
        {
            "edition": settings.edition,
            "company_name": company_name,
            "total": len(accounts),
            "groups": groups,
        },
    )


@router.get("/accounts/{account_id}", response_class=PlainTextResponse)
async def account_detail(account_id: str) -> str:
    return f"TODO: account detail for {account_id}"
