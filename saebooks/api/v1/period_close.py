"""JSON router — ``/api/v1/period-close``.

Backs the saebooks-web year-end-close page. Two endpoints:

* ``GET  /api/v1/period-close/preview``     — compute the zeroing entry
  WITHOUT posting (read-only).
* ``POST /api/v1/period-close/close-year``  — post the close journal and
  lock the period. Write-scoped (A2 token scopes gate POST automatically
  via ``require_bearer``); ``actor_role`` is resolved from the session so
  the close can post INTO the about-to-be-locked period (F-04), and an
  ``override_reason`` is required if ``through_date`` already falls in a
  locked range.

The close is an ADMIN action; the web layer gates the page to admins. The
heavy lifting lives in ``services.period_close`` — this is a thin JSON shell.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.journal_entries import _resolve_actor_role
from saebooks.services import period_close as period_close_svc
from saebooks.services.journal import PostingError

router = APIRouter(
    prefix="/period-close",
    tags=["period-close"],
    dependencies=[Depends(require_bearer)],
)


class ClosePreviewOut(BaseModel):
    through_date: date
    total_income: Decimal
    total_expenses: Decimal
    net_profit: Decimal
    has_anything_to_close: bool
    retained_earnings_debit: Decimal
    retained_earnings_credit: Decimal
    lines: list[dict[str, Any]]


class CloseYearRequest(BaseModel):
    through_date: date
    retained_earnings_account_id: UUID
    from_date: date | None = None
    override_reason: str | None = None


class CloseYearResult(BaseModel):
    closed: bool
    journal_entry_id: UUID | None = None


@router.get("/preview", response_model=ClosePreviewOut)
async def preview_close_year(
    request: Request,
    through_date: date = Query(...),
    retained_earnings_account_id: UUID = Query(...),
    from_date: date | None = Query(default=None),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Preview the year-end close (no state change)."""
    preview = await period_close_svc.preview_close(
        session,
        company_id,
        through_date=through_date,
        retained_earnings_account_id=retained_earnings_account_id,
        from_date=from_date,
    )
    return ClosePreviewOut(
        through_date=preview.through_date,
        total_income=preview.total_income,
        total_expenses=preview.total_expenses,
        net_profit=preview.net_profit,
        has_anything_to_close=preview.has_anything_to_close,
        retained_earnings_debit=preview.retained_earnings_debit,
        retained_earnings_credit=preview.retained_earnings_credit,
        lines=preview.lines,
    )


@router.post("/close-year", response_model=CloseYearResult)
async def close_year(
    request: Request,
    payload: CloseYearRequest,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Post the year-end close journal and lock the period.

    Returns ``closed=False`` when there is nothing to close (every P&L
    account already zero). 422 if ``through_date`` is in a locked range
    and no/insufficient override is supplied.
    """
    tenant_id = resolve_tenant_id(request)
    try:
        entry = await period_close_svc.close_year(
            session,
            company_id,
            tenant_id=tenant_id,
            through_date=payload.through_date,
            retained_earnings_account_id=payload.retained_earnings_account_id,
            from_date=payload.from_date,
            posted_by=f"api:{bearer[:8]}…",
            override_reason=payload.override_reason or None,
            actor_role=_resolve_actor_role(request),
        )
    except PostingError as exc:
        raise HTTPException(422, str(exc)) from exc

    if entry is None:
        return CloseYearResult(closed=False)
    return CloseYearResult(closed=True, journal_entry_id=entry.id)
