from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
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


def _build_hierarchy(accounts: list[Account]) -> list[dict[str, object]]:
    """Group accounts by first digit, with indent levels.

    Returns list of {"digit": str, "label": str, "rows": [{account, indent, anomaly}]}
    """
    by_digit: dict[str, list[Account]] = {}
    for a in accounts:
        digit = a.code[0] if a.code else "?"
        by_digit.setdefault(digit, []).append(a)

    groups = []
    for digit in sorted(by_digit.keys()):
        accts = by_digit[digit]
        label = svc.DIGIT_TO_LABEL.get(digit, f"Range {digit}xxx")

        # Find the shortest code in this group for base indent
        min_len = min(len(a.code) for a in accts)

        rows = []
        for a in accts:
            anomaly = svc.check_code_anomaly(a.code, a.account_type)
            rows.append({
                "account": a,
                "indent": len(a.code) - min_len,
                "anomaly": anomaly,
            })

        groups.append({
            "digit": digit,
            "label": label,
            "count": len(accts),
            "rows": rows,
        })

    return groups


@router.get("/accounts", response_class=HTMLResponse)
async def accounts_list(request: Request) -> HTMLResponse:
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        accounts = await svc.list_active(session, company.id)

    groups = _build_hierarchy(accounts)

    return templates.TemplateResponse(
        request,
        "accounts/list.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "total": len(accounts),
            "groups": groups,
            "account_types": ACCOUNT_TYPE_CHOICES,
            "digit_types": svc.DIGIT_TO_TYPES,
            "digit_labels": svc.DIGIT_TO_LABEL,
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
        # Validation error — re-render the page with the error
        async with AsyncSessionLocal() as session:
            accounts = await svc.list_active(session, company.id)
        groups = _build_hierarchy(accounts)
        return templates.TemplateResponse(
            request,
            "accounts/list.html",
            {
                "edition": settings.edition,
                "company_name": company.name,
                "total": len(accounts),
                "groups": groups,
                "account_types": ACCOUNT_TYPE_CHOICES,
                "digit_types": svc.DIGIT_TO_TYPES,
                "digit_labels": svc.DIGIT_TO_LABEL,
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
    return templates.TemplateResponse(
        request,
        "accounts/edit.html",
        {
            "edition": settings.edition,
            "company_name": company.name if company else "",
            "account": account,
            "account_types": ACCOUNT_TYPE_CHOICES,
            "digit_types": svc.DIGIT_TO_TYPES,
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
        return templates.TemplateResponse(
            request,
            "accounts/edit.html",
            {
                "edition": settings.edition,
                "company_name": company.name if company else "",
                "account": account,
                "account_types": ACCOUNT_TYPE_CHOICES,
                "digit_types": svc.DIGIT_TO_TYPES,
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
    """Show dependency report before deletion."""
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        deps = await svc.check_dependencies(session, account_id)

        # Get candidate accounts for migration (same type, not archived, not self)
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
    """Migrate all references to target account, then redirect to delete check."""
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
    """Hard-delete the account."""
    try:
        async with AsyncSessionLocal() as session:
            await svc.delete_account(session, account_id)
    except Exception as exc:
        return RedirectResponse(
            f"/accounts/{account_id}/delete?error={exc}",
            status_code=303,
        )
    return RedirectResponse("/accounts?deleted=1", status_code=303)
