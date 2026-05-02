"""Account ranges admin routes."""
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import AccountType
from saebooks.models.company import Company
from saebooks.models.user import UserRole
from saebooks.services import accounts as svc
from saebooks.services.authz import require_role
from saebooks.web import templates
from saebooks.services import active_company as active_svc

router = APIRouter(
    prefix="/admin/ranges",
    dependencies=[Depends(require_role(UserRole.ADMIN))],
)


ACCOUNT_TYPE_CHOICES = [(t.value, t.value.replace("_", " ").title()) for t in AccountType]


async def _first_company() -> Company:
    return await active_svc.first_company_compat()


@router.get("", response_class=HTMLResponse)
async def ranges_list(request: Request) -> HTMLResponse:
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        ranges = await svc.get_ranges(session, company.id)
        prefix_mode = await svc.get_prefix_mode(session)
    return templates.TemplateResponse(
        request,
        "admin/ranges.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "ranges": ranges,
            "account_types": ACCOUNT_TYPE_CHOICES,
            "prefix_mode": prefix_mode,
        },
    )


@router.post("", response_model=None)
async def ranges_create(
    request: Request,
    prefix: str = Form(...),
    label: str = Form(...),
    sort_order: int = Form(0),
) -> RedirectResponse | HTMLResponse:
    company = await _first_company()
    form = await request.form()

    # Collect checked account types
    account_types = [
        v for v, _ in ACCOUNT_TYPE_CHOICES
        if form.get(f"type_{v}")
    ]
    if not account_types:
        account_types = [AccountType.EQUITY.value]  # sensible default

    try:
        async with AsyncSessionLocal() as session:
            await svc.create_range(
                session,
                company.id,
                prefix=prefix,
                label=label,
                account_types=account_types,
                sort_order=sort_order,
            )
    except ValueError as exc:
        async with AsyncSessionLocal() as session:
            ranges = await svc.get_ranges(session, company.id)
        return templates.TemplateResponse(
            request,
            "admin/ranges.html",
            {
                "edition": settings.edition,
                "company_name": company.name,
                "ranges": ranges,
                "account_types": ACCOUNT_TYPE_CHOICES,
                "error": str(exc),
            },
            status_code=422,
        )
    return RedirectResponse("/admin/ranges", status_code=303)


@router.post("/{range_id}/delete")
async def ranges_delete(range_id: UUID) -> RedirectResponse:
    async with AsyncSessionLocal() as session:
        await svc.delete_range(session, range_id)
    return RedirectResponse("/admin/ranges", status_code=303)
