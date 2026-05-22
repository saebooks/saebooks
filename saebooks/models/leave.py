"""Leave balances + accrual events.

Per-employee per-leave-type running balance, with an append-only
accrual log. Accrual rule (NES baseline):

    ANNUAL    = 4 weeks/year   = 1/13 of ordinary hours worked
    PERSONAL  = 10 days/year  ≈ 1/26 of ordinary hours worked
    LONG_SERVICE = state-dependent, NOT auto-accrued in v1

Override rates per-employee via ``employees.extra`` JSONB (a
Phase 4.1 UX surfacing). For now the service uses defaults.

The balance row is the running total. The accrual row is the
audit log: ACCRUE on pay-run finalize, TAKE when a paid_leave_line
is consumed, ADJUST for manual corrections, PAYOUT for cash-out
on termination.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    Date,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class LeaveType(enum.StrEnum):
    ANNUAL = "ANNUAL"
    PERSONAL = "PERSONAL"
    LONG_SERVICE = "LONG_SERVICE"
    PARENTAL = "PARENTAL"
    OTHER = "OTHER"


class LeaveAccrualKind(enum.StrEnum):
    ACCRUE = "ACCRUE"
    TAKE = "TAKE"
    ADJUST = "ADJUST"
    PAYOUT = "PAYOUT"


class LeaveBalance(CompanyScoped, Base):
    __tablename__ = "leave_balances"
    __table_args__ = (
        UniqueConstraint(
            "employee_id", "leave_type",
            name="uq_leave_balances_employee_leave_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        default=lambda: _DEFAULT_TENANT_ID,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
    )
    leave_type: Mapped[str] = mapped_column(
        Enum(
            *[s.value for s in LeaveType],
            name="leave_type_enum",
            create_type=False,
        ),
        nullable=False,
    )
    balance_hours: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    opening_balance_hours: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    opening_balance_as_at: Mapped[date | None] = mapped_column(Date)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class LeaveAccrual(CompanyScoped, Base):
    __tablename__ = "leave_accruals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        default=lambda: _DEFAULT_TENANT_ID,
    )
    balance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leave_balances.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(
        Enum(
            *[s.value for s in LeaveAccrualKind],
            name="leave_accrual_kind_enum",
            create_type=False,
        ),
        nullable=False,
    )
    # Positive for ACCRUE / ADJUST(+); negative for TAKE / PAYOUT / ADJUST(-).
    hours: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    pay_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pay_runs.id", ondelete="SET NULL"),
    )
    pay_run_line_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pay_run_lines.id", ondelete="SET NULL"),
    )
    reason: Mapped[str | None] = mapped_column(Text)
    balance_after: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
