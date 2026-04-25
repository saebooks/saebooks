"""JSON router — ``/api/v1/search``.

Global read-only search across contacts, invoices, bills and accounts.

* Bearer-token auth via ``require_bearer``.
* Empty or whitespace-only ``q`` → 200 with empty hits list (no error).
* Results are capped per entity by ``search_svc.PER_ENTITY_LIMIT`` (10 each).
* No pagination — the palette never needs more than 40 hits total.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_session
from saebooks.api.v1.schemas import SearchHitOut, SearchResponse
from saebooks.models.company import Company
from saebooks.services import search as search_svc

router = APIRouter(
    prefix="/search",
    tags=["search"],
    dependencies=[Depends(require_bearer)],
)


async def _first_company_id(session: AsyncSession, tenant_id) -> str:
    """Return the first active company's UUID for this tenant."""
    result = await session.execute(
        select(Company)
        .where(
            Company.tenant_id == tenant_id,
            Company.archived_at.is_(None),
        )
        .order_by(Company.created_at)
    )
    company = result.scalars().first()
    if company is None:
        raise HTTPException(404, "No active company for tenant")
    return company.id


@router.get("", response_model=SearchResponse)
async def search(
    request: Request,
    q: str = Query(default="", description="Search query string"),
    session: AsyncSession = Depends(get_session),
) -> SearchResponse:
    """Search across contacts, invoices, bills and accounts.

    Returns up to 10 hits per entity type (40 total). An empty or
    whitespace-only ``q`` returns an empty hits list immediately.
    """
    query = (q or "").strip()

    tenant_id = resolve_tenant_id(request)
    company_id = await _first_company_id(session, tenant_id)
    hits = await search_svc.search_all(session, company_id, query=query)

    out_hits = [
        SearchHitOut(
            id=h.id,
            kind=h.kind,
            title=h.title,
            subtitle=h.subtitle,
            url=h.url,
        )
        for h in hits
    ]
    return SearchResponse(query=query, hits=out_hits, total=len(out_hits))
