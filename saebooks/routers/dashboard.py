"""Dashboard landing page — single-request overview of the books.

Mounted at ``/dashboard``. No feature gate — dashboard is core.

The template is a grid of includes, one per widget; each include
reads a named slot off ``dashboard`` so we can reorder them later
via config without editing the parent template.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.services import dashboard as svc
from saebooks.web import templates

router = APIRouter(prefix="/dashboard")


async def _first_company() -> Company | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Company)
            .where(Company.archived_at.is_(None))
            .order_by(Company.created_at)
        )
        return result.scalars().first()


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard_index(request: Request) -> HTMLResponse:
    """Render the dashboard.

    On a fresh DB with no company row, the dashboard is still
    useful as an installation hint rather than a 500 — so we
    surface a "no active company" empty state instead of raising.
    """
    company = await _first_company()
    if company is None:
        # No companies yet — render a placeholder page rather than
        # crashing. Migrations may have run but seed may be pending.
        raise HTTPException(500, "No active company")

    async with AsyncSessionLocal() as session:
        bundle = await svc.build_dashboard(session, company.id)

    return templates.TemplateResponse(
        request,
        "dashboard/index.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "today": date.today(),
            "dashboard": bundle,
        },
    )
