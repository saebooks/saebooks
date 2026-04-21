"""Inventory item routes.

CRUD + archive for tracked-stock items. Stock-movement history is
surfaced on the detail page (on-hand qty + WAC + the three GL
accounts). Actual stock movement happens via bill/invoice posts —
there's no direct "adjust stock" UI for v1 (use a manual journal
plus a negative-then-positive receipt if needed).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.item import CostMethod
from saebooks.services import items as svc
from saebooks.web import templates

router = APIRouter(prefix="/items")


async def _first_company() -> Company:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Company)
            .where(Company.archived_at.is_(None))
            .order_by(Company.created_at)
        )
        company = result.scalars().first()
        if company is None:
            raise HTTPException(500, "No active company")
        return company


async def _account_choices(
    company_id: UUID,
) -> tuple[list[Account], list[Account], list[Account]]:
    """Return (inventory asset, COGS, income) account choices for the picker."""
    async with AsyncSessionLocal() as session:
        stmt = select(Account).where(
            Account.company_id == company_id,
            Account.archived_at.is_(None),
        ).order_by(Account.code)
        result = await session.execute(stmt)
        all_accounts = list(result.scalars().all())
    inv = [a for a in all_accounts if a.account_type == AccountType.ASSET]
    cogs = [a for a in all_accounts if a.account_type == AccountType.COST_OF_SALES]
    income = [
        a
        for a in all_accounts
        if a.account_type in (AccountType.INCOME, AccountType.OTHER_INCOME)
    ]
    return inv, cogs, income


def _parse_decimal(raw: str, *, field: str, default: Decimal = Decimal("0")) -> Decimal:
    raw = (raw or "").strip()
    if not raw:
        return default
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise HTTPException(400, f"Invalid {field}: {raw}") from exc


# ---------------------------------------------------------------------- #
# List                                                                    #
# ---------------------------------------------------------------------- #


@router.get("", response_class=HTMLResponse)
async def items_list(
    request: Request,
    q: str | None = Query(None),
    archived: str | None = Query(None),
) -> HTMLResponse:
    company = await _first_company()
    include_archived = archived in ("1", "true", "on", "yes")
    async with AsyncSessionLocal() as session:
        items = await svc.list_items(
            session,
            company.id,
            search=q or None,
            include_archived=include_archived,
        )
    return templates.TemplateResponse(
        request,
        "items/list.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "items": items,
            "total": len(items),
            "include_archived": include_archived,
            "search_q": q or "",
        },
    )


# ---------------------------------------------------------------------- #
# Create                                                                  #
# ---------------------------------------------------------------------- #


@router.get("/new", response_class=HTMLResponse)
async def items_new(request: Request) -> HTMLResponse:
    company = await _first_company()
    inv, cogs, income = await _account_choices(company.id)
    return templates.TemplateResponse(
        request,
        "items/form.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "item": None,
            "error": None,
            "inventory_accounts": inv,
            "cogs_accounts": cogs,
            "income_accounts": income,
        },
    )


@router.post("", response_model=None)
async def items_create(
    request: Request,
    sku: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    inventory_account_id: str = Form(...),
    cogs_account_id: str = Form(...),
    income_account_id: str = Form(...),
    on_hand_qty: str = Form("0"),
    wac_cost: str = Form("0"),
    default_sale_price: str = Form("0"),
) -> RedirectResponse | HTMLResponse:
    company = await _first_company()
    try:
        async with AsyncSessionLocal() as session:
            item = await svc.create(
                session,
                company.id,
                sku=sku,
                name=name,
                description=description.strip() or None,
                inventory_account_id=UUID(inventory_account_id),
                cogs_account_id=UUID(cogs_account_id),
                income_account_id=UUID(income_account_id),
                cost_method=CostMethod.WAC,
                on_hand_qty=_parse_decimal(on_hand_qty, field="on_hand_qty"),
                wac_cost=_parse_decimal(wac_cost, field="wac_cost"),
                default_sale_price=_parse_decimal(
                    default_sale_price, field="default_sale_price"
                ),
            )
    except ValueError as exc:
        inv, cogs, income = await _account_choices(company.id)
        return templates.TemplateResponse(
            request,
            "items/form.html",
            {
                "edition": settings.edition,
                "company_name": company.name,
                "item": None,
                "error": str(exc),
                "inventory_accounts": inv,
                "cogs_accounts": cogs,
                "income_accounts": income,
            },
            status_code=422,
        )
    return RedirectResponse(f"/items/{item.id}", status_code=303)


# ---------------------------------------------------------------------- #
# Detail / Edit                                                           #
# ---------------------------------------------------------------------- #


@router.get("/{item_id}", response_class=HTMLResponse)
async def items_detail(request: Request, item_id: UUID) -> HTMLResponse:
    async with AsyncSessionLocal() as session:
        item = await svc.get(session, item_id)
        if item is None:
            raise HTTPException(404, "Item not found")
        company = await session.get(Company, item.company_id)
        inventory_account = await session.get(Account, item.inventory_account_id)
        cogs_account = await session.get(Account, item.cogs_account_id)
        income_account = await session.get(Account, item.income_account_id)
    return templates.TemplateResponse(
        request,
        "items/detail.html",
        {
            "edition": settings.edition,
            "company_name": company.name if company else "",
            "item": item,
            "inventory_account": inventory_account,
            "cogs_account": cogs_account,
            "income_account": income_account,
        },
    )


@router.get("/{item_id}/edit", response_class=HTMLResponse)
async def items_edit(request: Request, item_id: UUID) -> HTMLResponse:
    async with AsyncSessionLocal() as session:
        item = await svc.get(session, item_id)
        if item is None:
            raise HTTPException(404, "Item not found")
        company = await session.get(Company, item.company_id)
    inv, cogs, income = await _account_choices(item.company_id)
    return templates.TemplateResponse(
        request,
        "items/form.html",
        {
            "edition": settings.edition,
            "company_name": company.name if company else "",
            "item": item,
            "error": None,
            "inventory_accounts": inv,
            "cogs_accounts": cogs,
            "income_accounts": income,
        },
    )


@router.post("/{item_id}", response_model=None)
async def items_update(
    request: Request,
    item_id: UUID,
    sku: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    inventory_account_id: str = Form(...),
    cogs_account_id: str = Form(...),
    income_account_id: str = Form(...),
    default_sale_price: str = Form("0"),
) -> RedirectResponse | HTMLResponse:
    try:
        async with AsyncSessionLocal() as session:
            await svc.update(
                session,
                item_id,
                performed_by="web",
                sku=sku,
                name=name,
                description=description.strip() or None,
                inventory_account_id=UUID(inventory_account_id),
                cogs_account_id=UUID(cogs_account_id),
                income_account_id=UUID(income_account_id),
                default_sale_price=_parse_decimal(
                    default_sale_price, field="default_sale_price"
                ),
            )
    except ValueError as exc:
        async with AsyncSessionLocal() as session:
            item = await svc.get(session, item_id)
            if item is None:
                raise HTTPException(404, "Item not found") from exc
            company = await session.get(Company, item.company_id)
        inv, cogs, income = await _account_choices(item.company_id)
        return templates.TemplateResponse(
            request,
            "items/form.html",
            {
                "edition": settings.edition,
                "company_name": company.name if company else "",
                "item": item,
                "error": str(exc),
                "inventory_accounts": inv,
                "cogs_accounts": cogs,
                "income_accounts": income,
            },
            status_code=422,
        )
    return RedirectResponse(f"/items/{item_id}", status_code=303)


# ---------------------------------------------------------------------- #
# Archive                                                                 #
# ---------------------------------------------------------------------- #


@router.post("/{item_id}/archive")
async def items_archive(item_id: UUID) -> RedirectResponse:
    try:
        async with AsyncSessionLocal() as session:
            await svc.archive(session, item_id, performed_by="web")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse("/items", status_code=303)
