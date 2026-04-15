from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.account_range import AccountRange
from saebooks.models.company import Company
from saebooks.services import accounts as svc

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
) -> list[dict[str, object]]:
    """Group accounts by matched range, with indent levels.

    Accounts that don't match any range go into an "Unranged" group.
    """
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

        rows = []
        for a in accts:
            parsed = svc.parse_code(a.code, ranges)
            depth = parsed.depth if parsed else 0
            anomaly = svc.check_code_anomaly(a.code, a.account_type, ranges)
            rows.append({
                "account": a,
                "indent": depth,
                "anomaly": anomaly,
                "bustard": parsed.bustard if parsed else "",
            })

        groups.append({
            "prefix": rng.prefix,
            "label": rng.label,
            "count": len(accts),
            "rows": rows,
            "allowed_types": rng.account_types,
        })

    # Unranged accounts (legacy/seed data that doesn't match any range)
    if unranged:
        rows = []
        for a in unranged:
            rows.append({
                "account": a,
                "indent": 0,
                "anomaly": f"Code '{a.code}' doesn't match any defined range",
                "bustard": "",
            })
        groups.append({
            "prefix": "?",
            "label": "Unranged",
            "count": len(unranged),
            "rows": rows,
            "allowed_types": [],
        })

    return groups


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

    groups = _build_hierarchy(accounts, ranges)

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
        groups = _build_hierarchy(accounts, ranges)
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


@router.get("/accounts/{account_id}/edit", response_class=HTMLResponse)
async def accounts_edit(request: Request, account_id: UUID) -> HTMLResponse:
    async with AsyncSessionLocal() as session:
        account = await svc.get(session, account_id)
        if account is None:
            raise HTTPException(404, "Account not found")
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
        counts = await svc.migrate_account(session, account_id, target_id)

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
            await svc.delete_account(session, account_id)
    except Exception as exc:
        return RedirectResponse(
            f"/accounts/{account_id}/delete?error={exc}",
            status_code=303,
        )
    return RedirectResponse("/accounts?deleted=1", status_code=303)
