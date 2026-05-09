"""Companies management routes — list, switch, create.

The accounting model has always supported many companies per tenant
(see ``Company.tenant_id``), but the web UI before this router had no
way to see or pick a company: every form router called
``_first_company()`` and silently picked one ordered by ``created_at``.

This router exposes the missing surface:

* ``GET /companies`` — list every active company in the tenant, mark
  the one currently selected.
* ``POST /companies/switch/{id}`` — set the ``active_company_id``
  cookie and redirect back. Available on every edition because a
  Community user with one company still benefits from a clear "you
  are working in X" indicator.
* ``GET /companies/new`` + ``POST /companies`` — create a new
  company. Gated by ``FLAG_MULTI_COMPANY`` (Business+) — Community
  installs are licensed for one company per the charter.
* ``POST /companies/{id}/archive`` — soft-delete. Refuses to archive
  the last active company so a tenant can never end up companyless.

Tenant scoping
--------------
Every query filters by ``resolve_tenant_id(request)``. A user in
tenant A who fakes a UUID belonging to tenant B's company gets a 404
because the lookup constrains both id *and* tenant_id.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import resolve_tenant_id
from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.services import active_company as active_svc
from saebooks.services.features import FLAG_MULTI_COMPANY, is_enabled
from saebooks.web import templates

router = APIRouter(prefix="/companies")


@router.get("", response_class=HTMLResponse)
async def list_companies_page(request: Request) -> HTMLResponse:
    tenant_id = resolve_tenant_id(request)
    async with AsyncSessionLocal() as session:
        companies = await active_svc.list_companies(session, tenant_id)
        active = None
        if companies:
            active, _ = await active_svc.resolve_active_with_options(
                session, request, tenant_id
            )
    return templates.TemplateResponse(
        request,
        "companies/list.html",
        {
            "edition": settings.edition,
            "companies": companies,
            "active_company": active,
            "can_create": is_enabled(FLAG_MULTI_COMPANY),
        },
    )


@router.post("/switch/{company_id}", response_class=RedirectResponse)
async def switch_company(
    request: Request, company_id: uuid.UUID, next: str = "/dashboard"
) -> RedirectResponse:
    tenant_id = resolve_tenant_id(request)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Company).where(
                Company.id == company_id,
                Company.tenant_id == tenant_id,
                Company.archived_at.is_(None),
            )
        )
        company = result.scalars().first()
    if company is None:
        raise HTTPException(404, "Company not found")

    safe_next = next if next.startswith("/") else "/dashboard"
    response = RedirectResponse(url=safe_next, status_code=303)
    active_svc.set_active_cookie(response, company.id)
    return response


@router.get("/new", response_class=HTMLResponse)
async def new_company_form(request: Request) -> HTMLResponse:
    if not is_enabled(FLAG_MULTI_COMPANY):
        raise HTTPException(
            status_code=404,
            detail="Multi-company is a Business edition feature",
        )
    return templates.TemplateResponse(
        request,
        "companies/form.html",
        {"edition": settings.edition, "company": None},
    )


@router.post("", response_class=RedirectResponse)
async def create_company(
    request: Request,
    name: str = Form(...),
    legal_name: str = Form(""),
    abn: str = Form(""),
    base_currency: str = Form("AUD"),
    fin_year_start_month: int = Form(7),
    gst_registered: bool = Form(False),
) -> RedirectResponse:
    if not is_enabled(FLAG_MULTI_COMPANY):
        raise HTTPException(
            status_code=404,
            detail="Multi-company is a Business edition feature",
        )

    tenant_id = resolve_tenant_id(request)
    name = name.strip()
    if not name:
        raise HTTPException(422, "name is required")

    async with AsyncSessionLocal() as session:
        # Reject duplicate names within the tenant — multi-company UX
        # gets confusing when "Sauer Pty Ltd" and "Sauer Pty Ltd"
        # both appear in the switcher with no other distinguisher.
        existing = await session.execute(
            select(func.count(Company.id)).where(
                Company.tenant_id == tenant_id,
                Company.archived_at.is_(None),
                func.lower(Company.name) == name.lower(),
            )
        )
        if existing.scalar_one() > 0:
            raise HTTPException(422, f"A company named '{name}' already exists")

        company = Company(
            tenant_id=tenant_id,
            name=name,
            legal_name=legal_name.strip() or None,
            abn=abn.strip() or None,
            base_currency=base_currency.strip().upper() or "AUD",
            fin_year_start_month=fin_year_start_month or 7,
            gst_registered=bool(gst_registered),
        )
        session.add(company)
        await session.commit()
        await session.refresh(company)
        new_id = company.id

    response = RedirectResponse(url="/companies", status_code=303)
    # Switch to the new company immediately — that's almost always
    # what the user wants right after creating it.
    active_svc.set_active_cookie(response, new_id)
    return response


@router.post("/{company_id}/archive", response_class=RedirectResponse)
async def archive_company(
    request: Request, company_id: uuid.UUID
) -> RedirectResponse:
    tenant_id = resolve_tenant_id(request)
    async with AsyncSessionLocal() as session:
        # Refuse if it's the last active company in the tenant — a
        # tenant with zero companies has no usable UI.
        active_count = (
            await session.execute(
                select(func.count(Company.id)).where(
                    Company.tenant_id == tenant_id,
                    Company.archived_at.is_(None),
                )
            )
        ).scalar_one()
        if active_count <= 1:
            raise HTTPException(
                422, "Cannot archive the last active company"
            )

        result = await session.execute(
            select(Company).where(
                Company.id == company_id,
                Company.tenant_id == tenant_id,
                Company.archived_at.is_(None),
            )
        )
        company = result.scalars().first()
        if company is None:
            raise HTTPException(404, "Company not found")
        company.archived_at = func.now()
        await session.commit()

    return RedirectResponse(url="/companies", status_code=303)
