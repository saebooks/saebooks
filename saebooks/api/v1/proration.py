"""JSON router — ``/api/v1/proration``.

Surface
-------
* ``POST /preview``                    — generic per-line/date-range
                                          prorate (Prorate flow #3).
* ``POST /first-period-preview``       — first-period recurring prorate
                                          (Prorate flow #1) — preview only.
* ``POST /plan-change-preview``        — mid-period plan-change credit
                                          + charge (Prorate flow #2) —
                                          preview only.
* ``POST /deferred-revenue/preview``   — what would
                                          ``recognize_deferred_revenue``
                                          post for ``period_date``?
                                          (Prorate flow #4 preview).
* ``POST /deferred-revenue/recognize`` — actually post the monthly
                                          amortisation JE (Prorate flow #4).

All routes are bearer-gated. The preview routes are pure math —
they touch no DB rows. ``deferred-revenue/recognize`` writes a posted
journal entry and stamps ``recognized_through_date`` on the included
invoice lines, exactly the same transaction the existing
``recognize_deferred_revenue`` service already does.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.schemas import (
    DeferredRevenuePreviewBody,
    DeferredRevenuePreviewLine,
    DeferredRevenuePreviewOut,
    DeferredRevenueRecognizeBody,
    DeferredRevenueRecognizeOut,
    FirstPeriodPreviewBody,
    FirstPeriodPreviewOut,
    PlanChangePreviewBody,
    PlanChangePreviewOut,
    ProratePreviewBody,
    ProratePreviewOut,
)
from saebooks.services import deferred_revenue as dr_svc
from saebooks.services import proration as pr

router = APIRouter(
    prefix="/proration",
    tags=["proration"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Generic per-line / date-range preview (Prorate #3)
# ---------------------------------------------------------------------------


@router.post("/preview", response_model=ProratePreviewOut)
async def preview_prorate(payload: ProratePreviewBody) -> ProratePreviewOut:
    try:
        basis = pr.basis_from_string(payload.basis)
    except pr.ProrationError as exc:
        raise HTTPException(422, str(exc)) from exc

    try:
        days_used = pr.days_inclusive(payload.service_start, payload.service_end)
        days_in_full = pr.days_in_basis_period(basis, payload.service_start)
        factor = pr.prorate_factor(basis, payload.service_start, payload.service_end)
        prorated = pr.prorate_amount(
            payload.full_period_amount,
            basis,
            payload.service_start,
            payload.service_end,
        )
    except pr.ProrationError as exc:
        raise HTTPException(422, str(exc)) from exc

    return ProratePreviewOut(
        full_period_amount=payload.full_period_amount,
        basis=basis.value,
        service_start=payload.service_start,
        service_end=payload.service_end,
        days_used=days_used,
        days_in_full=days_in_full,
        factor=factor,
        prorated_amount=prorated,
    )


# ---------------------------------------------------------------------------
# First-period recurring (Prorate #1) — preview only
# ---------------------------------------------------------------------------


@router.post("/first-period-preview", response_model=FirstPeriodPreviewOut)
async def preview_first_period(
    payload: FirstPeriodPreviewBody,
) -> FirstPeriodPreviewOut:
    try:
        basis = pr.basis_from_string(payload.basis)
        result = pr.first_period_prorate(
            payload.full_period_amount,
            basis,
            payload.service_start,
            payload.service_end,
        )
    except pr.ProrationError as exc:
        raise HTTPException(422, str(exc)) from exc

    return FirstPeriodPreviewOut(
        full_period_amount=result.full_period_amount,
        basis=result.basis.value,
        service_start=result.service_start,
        service_end=result.service_end,
        days_used=result.days_used,
        days_in_full=result.days_in_full,
        factor=result.factor,
        prorated_amount=result.prorated_amount,
        line_description_suggestion=(
            f"Pro-rata {result.days_used} of {result.days_in_full} days "
            f"({result.service_start.isoformat()} – "
            f"{result.service_end.isoformat()})"
        ),
    )


# ---------------------------------------------------------------------------
# Mid-period plan-change (Prorate #2) — preview only
# ---------------------------------------------------------------------------


@router.post("/plan-change-preview", response_model=PlanChangePreviewOut)
async def preview_plan_change(
    payload: PlanChangePreviewBody,
) -> PlanChangePreviewOut:
    try:
        adj = pr.plan_change_adjustment(
            payload.old_period_amount,
            payload.new_period_amount,
            payload.period_start,
            payload.period_end,
            payload.change_date,
        )
    except pr.ProrationError as exc:
        raise HTTPException(422, str(exc)) from exc

    return PlanChangePreviewOut(
        period_start=adj.period_start,
        period_end=adj.period_end,
        change_date=adj.change_date,
        days_total=adj.days_total,
        days_used=adj.days_used,
        days_remaining=adj.days_remaining,
        credit=adj.credit,
        charge=adj.charge,
        net=adj.net,
    )


# ---------------------------------------------------------------------------
# Deferred-revenue (Prorate #4) — preview + recognise
# ---------------------------------------------------------------------------


@router.post("/deferred-revenue/preview", response_model=DeferredRevenuePreviewOut)
async def preview_deferred_revenue(
    payload: DeferredRevenuePreviewBody,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> DeferredRevenuePreviewOut:
    preview = await dr_svc.preview_deferred_recognition(
        session, company_id, payload.period_date
    )
    return DeferredRevenuePreviewOut(
        period_first=preview.period_first,
        total_recognized=preview.total_recognized,
        lines=[
            DeferredRevenuePreviewLine(
                invoice_line_id=row["invoice_line_id"],
                invoice_number=str(row["invoice_number"]),
                description=str(row["description"]),
                income_account_id=row["income_account_id"],
                amount=row["amount"],
            )
            for row in preview.lines
        ],
    )


@router.post(
    "/deferred-revenue/recognize", response_model=DeferredRevenueRecognizeOut
)
async def recognize_deferred_revenue(
    payload: DeferredRevenueRecognizeBody,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> DeferredRevenueRecognizeOut:
    # Capture preview before posting so we can return a structured
    # response listing what was recognised.
    preview = await dr_svc.preview_deferred_recognition(
        session, company_id, payload.period_date
    )

    if not preview.has_entries:
        return DeferredRevenueRecognizeOut(
            period_first=preview.period_first,
            total_recognized=Decimal("0"),
            lines_recognized=0,
            posted=False,
        )

    try:
        await dr_svc.recognize_deferred_revenue(
            session,
            company_id,
            payload.period_date,
            tenant_id=resolve_tenant_id(request),
            posted_by=f"api:{bearer[:8]}…",
            override_reason=payload.override_reason,
        )
    except dr_svc.DeferredRevenueError as exc:
        raise HTTPException(422, str(exc)) from exc

    return DeferredRevenueRecognizeOut(
        period_first=preview.period_first,
        total_recognized=preview.total_recognized,
        lines_recognized=len(preview.lines),
        posted=True,
    )
