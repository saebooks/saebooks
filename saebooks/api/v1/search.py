"""JSON router — ``/api/v1/search``.

Global read-only search across contacts, invoices, bills and accounts.

* Bearer-token auth via ``require_bearer``.
* Empty or whitespace-only ``q`` → 200 with empty hits list (no error).
* Results are capped per entity by ``search_svc.PER_ENTITY_LIMIT`` (10 each).
* No pagination — the palette never needs more than 40 hits total.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.schemas import SearchHitOut, SearchResponse
from saebooks.services import search as search_svc

router = APIRouter(
    prefix="/search",
    tags=["search"],
    dependencies=[Depends(require_bearer)],
)


@router.get("", response_model=SearchResponse)
async def search(
    q: str = Query(default="", description="Search query string"),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> SearchResponse:
    """Search across contacts, invoices, bills and accounts.

    Returns up to 10 hits per entity type (40 total). An empty or
    whitespace-only ``q`` returns an empty hits list immediately.

    The active company is resolved by ``get_active_company_id`` —
    callers may pin a specific company via the ``X-Company-Id`` header;
    otherwise the first active company for the tenant is used.
    """
    query = (q or "").strip()

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
