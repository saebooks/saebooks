"""Global search + keyboard-shortcut help pages.

Two routes:

* ``GET /search`` — returns an HTMX fragment (``search/_results.html``)
  when the ``HX-Request`` header is set, else the full page. The Cmd-K
  palette injects itself into a ``<dialog>`` and swaps the results
  fragment on every keystroke.
* ``GET /help/shortcuts`` — static page listing the shortcuts. Lives
  under this router because it documents the same feature.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import settings
from saebooks.models.company import Company
from saebooks.routers.deps import get_web_session
from saebooks.services import active_company as active_svc
from saebooks.services import search as svc
from saebooks.web import templates

router = APIRouter()


async def _first_company() -> Company:
    return await active_svc.first_company_compat()


@router.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: str = Query(""),
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    """Full-page search, or HTMX fragment when the header is set."""
    company = await _first_company()
    hits = await svc.search_all(session, company.id, q)

    template_name = (
        "search/_results.html"
        if request.headers.get("hx-request") == "true"
        else "search/index.html"
    )
    return templates.TemplateResponse(
        request,
        template_name,
        {
            "edition": settings.edition,
            "q": q,
            "hits": hits,
        },
    )


@router.get("/help/shortcuts", response_class=HTMLResponse)
async def shortcuts_help(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "help/shortcuts.html",
        {"edition": settings.edition},
    )
