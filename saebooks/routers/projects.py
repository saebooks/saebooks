"""Project (job) routes.

Thin CRUD + archive surface mirroring ``routers/contacts.py``. Project
rows are per-company tags stamped on invoice / bill / journal lines for
P&L-by-project reports in ``services/reports.py``.
"""
from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from saebooks.api.v1.auth import resolve_tenant_id
from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.project import ProjectStatus
from saebooks.services import projects as svc
from saebooks.web import templates

router = APIRouter(prefix="/projects")


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


def _parse_status(raw: str | None) -> ProjectStatus | None:
    if not raw:
        return None
    if raw.upper() in ProjectStatus.__members__:
        return ProjectStatus(raw.upper())
    return None


def _parse_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(400, f"Invalid date: {raw}") from exc


# ---------------------------------------------------------------------- #
# List                                                                    #
# ---------------------------------------------------------------------- #


@router.get("", response_class=HTMLResponse)
async def projects_list(
    request: Request,
    status: str | None = Query(None),
    q: str | None = Query(None),
    archived: str | None = Query(None),
) -> HTMLResponse:
    company = await _first_company()
    filter_status = _parse_status(status)
    include_archived = archived in ("1", "true", "on", "yes")
    async with AsyncSessionLocal() as session:
        projects = await svc.list_active(
            session,
            company.id,
            status=filter_status,
            search=q or None,
            include_archived=include_archived,
        )
    return templates.TemplateResponse(
        request,
        "projects/list.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "projects": projects,
            "total": len(projects),
            "status_filter": status or "all",
            "include_archived": include_archived,
            "search_q": q or "",
        },
    )


# ---------------------------------------------------------------------- #
# Create                                                                  #
# ---------------------------------------------------------------------- #


@router.get("/new", response_class=HTMLResponse)
async def projects_new(request: Request) -> HTMLResponse:
    company = await _first_company()
    return templates.TemplateResponse(
        request,
        "projects/form.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "project": None,
            "error": None,
            "statuses": [s.value for s in ProjectStatus],
        },
    )


@router.post("", response_model=None)
async def projects_create(
    request: Request,
    code: str = Form(...),
    name: str = Form(...),
    status: str = Form("ACTIVE"),
    start_date: str = Form(""),
    end_date: str = Form(""),
    notes: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    company = await _first_company()
    try:
        status_enum = ProjectStatus(status)
    except ValueError as exc:
        raise HTTPException(400, f"Invalid status: {status}") from exc
    try:
        async with AsyncSessionLocal() as session:
            project = await svc.create(
                session,
                company.id,
                code=code,
                name=name,
                status=status_enum,
                start_date=_parse_date(start_date),
                end_date=_parse_date(end_date),
                notes=notes.strip() or None,
            )
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "projects/form.html",
            {
                "edition": settings.edition,
                "company_name": company.name,
                "project": None,
                "error": str(exc),
                "statuses": [s.value for s in ProjectStatus],
            },
            status_code=422,
        )
    return RedirectResponse(f"/projects/{project.id}", status_code=303)


# ---------------------------------------------------------------------- #
# Detail / Edit                                                           #
# ---------------------------------------------------------------------- #


@router.get("/{project_id}", response_class=HTMLResponse)
async def projects_detail(request: Request, project_id: UUID) -> HTMLResponse:
    tenant_id = resolve_tenant_id(request)
    async with AsyncSessionLocal() as session:
        project = await svc.get(session, project_id, tenant_id=tenant_id)
        if project is None:
            raise HTTPException(404, "Project not found")
        company = await session.get(Company, project.company_id)
    return templates.TemplateResponse(
        request,
        "projects/detail.html",
        {
            "edition": settings.edition,
            "company_name": company.name if company else "",
            "project": project,
        },
    )


@router.get("/{project_id}/edit", response_class=HTMLResponse)
async def projects_edit(request: Request, project_id: UUID) -> HTMLResponse:
    tenant_id = resolve_tenant_id(request)
    async with AsyncSessionLocal() as session:
        project = await svc.get(session, project_id, tenant_id=tenant_id)
        if project is None:
            raise HTTPException(404, "Project not found")
        company = await session.get(Company, project.company_id)
    return templates.TemplateResponse(
        request,
        "projects/form.html",
        {
            "edition": settings.edition,
            "company_name": company.name if company else "",
            "project": project,
            "error": None,
            "statuses": [s.value for s in ProjectStatus],
        },
    )


@router.post("/{project_id}", response_model=None)
async def projects_update(
    request: Request,
    project_id: UUID,
    code: str = Form(...),
    name: str = Form(...),
    status: str = Form("ACTIVE"),
    start_date: str = Form(""),
    end_date: str = Form(""),
    notes: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    try:
        status_enum = ProjectStatus(status)
    except ValueError as exc:
        raise HTTPException(400, f"Invalid status: {status}") from exc
    tenant_id = resolve_tenant_id(request)
    try:
        async with AsyncSessionLocal() as session:
            await svc.update(
                session,
                project_id,
                tenant_id=tenant_id,
                performed_by="web",
                code=code,
                name=name,
                status=status_enum,
                start_date=_parse_date(start_date),
                end_date=_parse_date(end_date),
                notes=notes.strip() or None,
            )
    except ValueError as exc:
        async with AsyncSessionLocal() as session:
            project = await svc.get(session, project_id, tenant_id=tenant_id)
            if project is None:
                raise HTTPException(404, "Project not found") from exc
            company = await session.get(Company, project.company_id)
        return templates.TemplateResponse(
            request,
            "projects/form.html",
            {
                "edition": settings.edition,
                "company_name": company.name if company else "",
                "project": project,
                "error": str(exc),
                "statuses": [s.value for s in ProjectStatus],
            },
            status_code=422,
        )
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


# ---------------------------------------------------------------------- #
# Archive                                                                 #
# ---------------------------------------------------------------------- #


@router.post("/{project_id}/archive")
async def projects_archive(request: Request, project_id: UUID) -> RedirectResponse:
    tenant_id = resolve_tenant_id(request)
    async with AsyncSessionLocal() as session:
        await svc.archive(session, project_id, tenant_id=tenant_id, performed_by="web")
    return RedirectResponse("/projects", status_code=303)
