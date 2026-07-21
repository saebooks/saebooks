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

Period-locks CRUD
------------------
Posting already enforces ``max(locked_through)`` per company
(``services.journal._check_period_lock``) against every ``PeriodLock`` row,
regardless of how it was created. ``close-year`` above is one way to create
one; the three routes below (``POST``/``GET``/``DELETE``
``/period-close/locks``) let an admin lock (or unlock) a period
independently of a year-end close — e.g. to lock a completed quarter.
Admin-only (``_require_admin``, added on these three routes only — the
pre-existing preview/close-year gating is untouched).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_active_user_id, get_session
from saebooks.api.v1.journal_entries import _resolve_actor_role
from saebooks.api.v1.users import _require_admin
from saebooks.models.journal import PeriodLock
from saebooks.services import audit_log as audit_log_svc
from saebooks.services import journal as journal_svc
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


# ---------------------------------------------------------------------------
# Period-locks CRUD — admin-only, independent of year-end close
# ---------------------------------------------------------------------------


class PeriodLockCreate(BaseModel):
    locked_through: date
    reason: str | None = None


class PeriodLockOut(BaseModel):
    id: UUID
    locked_through: date
    locked_at: datetime
    locked_by: str | None = None
    reason: str | None = None


class PeriodLockListOut(BaseModel):
    items: list[PeriodLockOut]
    effective_locked_through: date | None = None


@router.post(
    "/locks",
    response_model=PeriodLockOut,
    status_code=201,
    dependencies=[Depends(_require_admin)],
)
async def create_period_lock(
    request: Request,
    payload: PeriodLockCreate,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Lock the company through ``locked_through`` (admin-only).

    409 if ``locked_through`` does not extend beyond the company's current
    ``max(locked_through)`` — a lock that doesn't move the effective
    boundary forward is a confusing no-op for enforcement, so it is
    rejected rather than silently accepted.

    The monotonicity check and the insert run in the same transaction,
    with the company's existing ``PeriodLock`` rows locked ``FOR UPDATE``
    first — otherwise two concurrent non-advancing POSTs can each read the
    same stale ``current`` before either commits, and both pass the 409
    guard.
    """
    locked_through_values = (
        await session.execute(
            select(PeriodLock.locked_through)
            .where(PeriodLock.company_id == company_id)
            .with_for_update()
        )
    ).scalars().all()
    current = max(locked_through_values, default=None)
    if current is not None and payload.locked_through <= current:
        raise HTTPException(
            409,
            f"locked_through {payload.locked_through} does not extend beyond "
            f"the current lock ({current}); pick a later date",
        )

    lock = await journal_svc.lock_period(
        session,
        company_id,
        payload.locked_through,
        locked_by=f"api:{bearer[:8]}…",
        reason=payload.reason,
    )
    return PeriodLockOut(
        id=lock.id,
        locked_through=lock.locked_through,
        locked_at=lock.locked_at,
        locked_by=lock.locked_by,
        reason=lock.reason,
    )


@router.get(
    "/locks",
    response_model=PeriodLockListOut,
    dependencies=[Depends(_require_admin)],
)
async def list_period_locks(
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """List the company's lock history newest-first (admin-only).

    ``effective_locked_through`` is the max ``locked_through`` across all
    lock rows (or ``null`` if there are none) — the date posting is
    actually enforced against right now.
    """
    rows = (
        await session.execute(
            select(PeriodLock)
            .where(PeriodLock.company_id == company_id)
            .order_by(PeriodLock.locked_at.desc())
        )
    ).scalars().all()
    effective = await journal_svc.get_locked_through(session, company_id)
    return PeriodLockListOut(
        items=[
            PeriodLockOut(
                id=r.id,
                locked_through=r.locked_through,
                locked_at=r.locked_at,
                locked_by=r.locked_by,
                reason=r.reason,
            )
            for r in rows
        ],
        effective_locked_through=effective,
    )


@router.delete(
    "/locks/{lock_id}",
    status_code=204,
    dependencies=[Depends(_require_admin)],
)
async def delete_period_lock(
    request: Request,
    lock_id: uuid.UUID,
    reason: str = Query(..., description="Required forensic reason for removing this lock"),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Response:
    """Remove a period-lock row (admin-only).

    Enforcement recedes to the remaining ``max(locked_through)`` (or fully
    open if this was the last lock) — a shorter/older lock elsewhere is
    NOT re-applied, the boundary simply moves to whatever locks remain.

    A ``reason`` query parameter is required (422 if missing or blank) and
    is written, together with a full snapshot of the removed row, to
    ``audit_log`` (action ``period_lock.delete``) in the SAME transaction
    as the delete — this is a forensic record of "who unlocked what and
    why", not a routine list/read.

    404 if the lock doesn't exist or belongs to another company.
    """
    if not reason.strip():
        raise HTTPException(422, "reason is required and must not be blank")

    lock = await session.get(PeriodLock, lock_id)
    if lock is None or lock.company_id != company_id:
        raise HTTPException(404, "Period lock not found")

    tenant_id = resolve_tenant_id(request)
    actor_user_id = await get_active_user_id(request)
    snapshot = jsonable_encoder(
        {c.key: getattr(lock, c.key) for c in lock.__table__.columns}
    )
    await audit_log_svc.append(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action=audit_log_svc.AuditAction.PERIOD_LOCK_DELETE,
        table_name="period_locks",
        row_id=str(lock.id),
        row_snapshot=snapshot,
        reason=reason,
    )
    await session.delete(lock)
    await session.commit()
    return Response(status_code=204)
