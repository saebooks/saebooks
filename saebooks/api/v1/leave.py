"""JSON router — ``/api/v1/leave``.

Per-employee leave balances + manual adjustments + opening-balance
seeding. Accruals fire automatically on pay-run finalize (Phase 2
service); this router is for the read + manual-adjust surface.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.models.leave import LeaveBalance
from saebooks.services import leave as svc
from saebooks.services.leave import LeaveError

router = APIRouter(
    prefix="/leave",
    tags=["leave"],
    dependencies=[Depends(require_bearer)],
)


class LeaveBalanceOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    leave_type: str
    balance_hours: Decimal
    opening_balance_hours: Decimal
    opening_balance_as_at: str | None = None
    version: int


class LeaveAdjustIn(BaseModel):
    leave_type: str
    delta_hours: Decimal
    reason: str = Field(min_length=1)


class LeaveOpeningBalanceIn(BaseModel):
    leave_type: str
    hours: Decimal
    as_at: str | None = None


def _to_dto(b: LeaveBalance) -> LeaveBalanceOut:
    return LeaveBalanceOut(
        id=b.id,
        employee_id=b.employee_id,
        leave_type=b.leave_type,
        balance_hours=b.balance_hours,
        opening_balance_hours=b.opening_balance_hours,
        opening_balance_as_at=(
            b.opening_balance_as_at.isoformat() if b.opening_balance_as_at else None
        ),
        version=b.version,
    )


@router.get("/balances/{employee_id}", response_model=list[LeaveBalanceOut])
async def get_balances(
    employee_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> list[LeaveBalanceOut]:
    balances = await svc.get_balances(
        session, company_id=company_id, employee_id=employee_id
    )
    return [_to_dto(b) for b in balances]


@router.post("/balances/{employee_id}/adjust", response_model=LeaveBalanceOut)
async def adjust_balance(
    employee_id: uuid.UUID,
    body: LeaveAdjustIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> LeaveBalanceOut:
    tenant_id = resolve_tenant_id(request)
    created_by = None
    user = getattr(request.state, "user", None)
    if user is not None and getattr(user, "id", None):
        created_by = user.id
    try:
        balance = await svc.adjust(
            session,
            company_id=company_id,
            tenant_id=tenant_id,
            employee_id=employee_id,
            leave_type=body.leave_type,
            delta_hours=body.delta_hours,
            reason=body.reason,
            created_by=created_by,
        )
        await session.commit()
    except LeaveError as exc:
        if exc.code == "missing_reason":
            raise HTTPException(400, str(exc)) from exc
        raise HTTPException(400, str(exc)) from exc
    return _to_dto(balance)


@router.post(
    "/balances/{employee_id}/opening", response_model=LeaveBalanceOut
)
async def set_opening_balance(
    employee_id: uuid.UUID,
    body: LeaveOpeningBalanceIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> LeaveBalanceOut:
    from datetime import datetime as _dt

    tenant_id = resolve_tenant_id(request)
    as_at_dt = None
    if body.as_at:
        try:
            as_at_dt = _dt.fromisoformat(body.as_at)
        except ValueError as exc:
            raise HTTPException(400, "as_at must be ISO 8601 date") from exc
    balance = await svc.set_opening_balance(
        session,
        company_id=company_id,
        tenant_id=tenant_id,
        employee_id=employee_id,
        leave_type=body.leave_type,
        hours=body.hours,
        as_at=as_at_dt,
    )
    await session.commit()
    return _to_dto(balance)
