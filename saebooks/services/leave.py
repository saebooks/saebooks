"""Leave accrual + balance service.

Two main entry points:

- ``accrue_on_pay_run_line(...)`` — called from the pay-run finalize
  flow. Computes ANNUAL + PERSONAL accruals from ordinary hours
  and writes an ACCRUE row + bumps the balance.
- ``take(...)`` — called when a pay-run line includes paid leave.
  Validates sufficient balance, writes a TAKE row, decrements
  the balance.

Default accrual rates per NES (override via employees.extra in v1.1):

    ANNUAL   = 1/13 of ordinary hours worked
    PERSONAL = 1/26 of ordinary hours worked
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.leave import LeaveAccrual, LeaveAccrualKind, LeaveBalance, LeaveType
from saebooks.money import round_money

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# NES baseline rates (overridable per-employee in v1.1).
_ANNUAL_PER_OT_HOUR = Decimal("1") / Decimal("13")   # 4 weeks / 52 weeks
_PERSONAL_PER_OT_HOUR = Decimal("1") / Decimal("26")  # 10 days / 260 days


class LeaveError(Exception):
    def __init__(self, message: str, *, code: str = "leave_error") -> None:
        super().__init__(message)
        self.code = code


def _q2(value: Decimal) -> Decimal:
    return round_money(value)


@dataclass
class AccrualResult:
    annual_hours: Decimal
    personal_hours: Decimal


async def _get_or_create_balance(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    employee_id: uuid.UUID,
    leave_type: LeaveType | str,
    tenant_id: uuid.UUID = _DEFAULT_TENANT_ID,
) -> LeaveBalance:
    leave_type_v = (
        leave_type.value if hasattr(leave_type, "value") else str(leave_type)
    )
    stmt = sa.select(LeaveBalance).where(
        LeaveBalance.company_id == company_id,
        LeaveBalance.employee_id == employee_id,
        LeaveBalance.leave_type == leave_type_v,
    )
    balance = (await session.execute(stmt)).scalar_one_or_none()
    if balance is not None:
        return balance
    balance = LeaveBalance(
        company_id=company_id,
        tenant_id=tenant_id,
        employee_id=employee_id,
        leave_type=leave_type_v,
        balance_hours=Decimal("0"),
        opening_balance_hours=Decimal("0"),
    )
    session.add(balance)
    await session.flush()
    await session.refresh(balance)
    return balance


async def get_balances(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    employee_id: uuid.UUID,
) -> list[LeaveBalance]:
    stmt = (
        sa.select(LeaveBalance)
        .where(
            LeaveBalance.company_id == company_id,
            LeaveBalance.employee_id == employee_id,
        )
        .order_by(LeaveBalance.leave_type)
    )
    return list((await session.execute(stmt)).scalars().all())


async def accrue_on_pay_run_line(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    employee_id: uuid.UUID,
    pay_run_id: uuid.UUID,
    pay_run_line_id: uuid.UUID,
    ordinary_hours: Decimal,
    tenant_id: uuid.UUID = _DEFAULT_TENANT_ID,
) -> AccrualResult:
    """Add the per-hour-worked accrual to ANNUAL + PERSONAL balances.

    OVERTIME hours are excluded (per NES).
    """
    annual = _q2(ordinary_hours * _ANNUAL_PER_OT_HOUR)
    personal = _q2(ordinary_hours * _PERSONAL_PER_OT_HOUR)

    for (leave_type, hours) in (
        (LeaveType.ANNUAL, annual),
        (LeaveType.PERSONAL, personal),
    ):
        if hours <= 0:
            continue
        balance = await _get_or_create_balance(
            session,
            company_id=company_id,
            employee_id=employee_id,
            leave_type=leave_type,
            tenant_id=tenant_id,
        )
        balance.balance_hours = _q2(balance.balance_hours + hours)
        balance.version += 1
        accrual = LeaveAccrual(
            company_id=company_id,
            tenant_id=tenant_id,
            balance_id=balance.id,
            kind=LeaveAccrualKind.ACCRUE.value,
            hours=hours,
            pay_run_id=pay_run_id,
            pay_run_line_id=pay_run_line_id,
            reason=None,
            balance_after=balance.balance_hours,
        )
        session.add(accrual)
    await session.flush()
    return AccrualResult(annual_hours=annual, personal_hours=personal)


async def take(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    employee_id: uuid.UUID,
    leave_type: LeaveType | str,
    hours: Decimal,
    pay_run_id: uuid.UUID | None = None,
    pay_run_line_id: uuid.UUID | None = None,
    reason: str | None = None,
    tenant_id: uuid.UUID = _DEFAULT_TENANT_ID,
    allow_negative: bool = False,
) -> LeaveBalance:
    if hours <= 0:
        raise LeaveError("hours must be > 0", code="invalid_hours")
    balance = await _get_or_create_balance(
        session,
        company_id=company_id,
        employee_id=employee_id,
        leave_type=leave_type,
        tenant_id=tenant_id,
    )
    new_balance = _q2(balance.balance_hours - hours)
    if new_balance < 0 and not allow_negative:
        raise LeaveError(
            f"insufficient {leave_type} balance: have {balance.balance_hours}h, want {hours}h",
            code="insufficient_balance",
        )
    balance.balance_hours = new_balance
    balance.version += 1
    accrual = LeaveAccrual(
        company_id=company_id,
        tenant_id=tenant_id,
        balance_id=balance.id,
        kind=LeaveAccrualKind.TAKE.value,
        hours=-hours,  # negative — consumption
        pay_run_id=pay_run_id,
        pay_run_line_id=pay_run_line_id,
        reason=reason,
        balance_after=balance.balance_hours,
    )
    session.add(accrual)
    await session.flush()
    return balance


async def adjust(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    employee_id: uuid.UUID,
    leave_type: LeaveType | str,
    delta_hours: Decimal,
    reason: str,
    created_by: uuid.UUID | None = None,
    tenant_id: uuid.UUID = _DEFAULT_TENANT_ID,
) -> LeaveBalance:
    """Manual adjustment. delta can be positive or negative."""
    if not reason.strip():
        raise LeaveError("reason required for manual adjust", code="missing_reason")
    balance = await _get_or_create_balance(
        session,
        company_id=company_id,
        employee_id=employee_id,
        leave_type=leave_type,
        tenant_id=tenant_id,
    )
    balance.balance_hours = _q2(balance.balance_hours + delta_hours)
    balance.version += 1
    accrual = LeaveAccrual(
        company_id=company_id,
        tenant_id=tenant_id,
        balance_id=balance.id,
        kind=LeaveAccrualKind.ADJUST.value,
        hours=delta_hours,
        reason=reason.strip(),
        balance_after=balance.balance_hours,
        created_by=created_by,
    )
    session.add(accrual)
    await session.flush()
    return balance


async def set_opening_balance(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    employee_id: uuid.UUID,
    leave_type: LeaveType | str,
    hours: Decimal,
    as_at: datetime | None = None,
    tenant_id: uuid.UUID = _DEFAULT_TENANT_ID,
) -> LeaveBalance:
    """Set opening balance — used to migrate prior-system leave data.

    Idempotent: re-setting overwrites the opening value but leaves the
    current balance alone (use ``adjust`` if the running balance needs
    rewriting separately).
    """
    balance = await _get_or_create_balance(
        session,
        company_id=company_id,
        employee_id=employee_id,
        leave_type=leave_type,
        tenant_id=tenant_id,
    )
    balance.opening_balance_hours = _q2(hours)
    if as_at is not None:
        balance.opening_balance_as_at = as_at
    if balance.balance_hours == 0:
        # First-time set — also seed the running balance.
        balance.balance_hours = _q2(hours)
    balance.version += 1
    await session.flush()
    return balance
