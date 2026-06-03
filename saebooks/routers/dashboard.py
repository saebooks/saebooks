"""Dashboard landing page — single-request overview of the books.

Mounted at ``/dashboard``. No feature gate — dashboard is core.

The template is a grid of includes, one per widget; each include
reads a named slot off ``dashboard`` so we can reorder them later
via config without editing the parent template.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import settings
from saebooks.models.company import Company
from saebooks.routers.deps import get_web_session
from saebooks.services import dashboard as svc
from saebooks.web import templates
from saebooks.services import active_company as active_svc

router = APIRouter(prefix="/dashboard")


async def _first_company() -> Company | None:
    return await active_svc.first_company_compat_or_none()


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard_index(
    request: Request,
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
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
