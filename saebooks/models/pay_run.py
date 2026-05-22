"""PayRun and PayRunLine ORM models -- Cat-C community-tier.

Lifecycle: draft -> aba_exported -> finalized
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from typing import Any

from sqlalchemy import (
    Date, DateTime, ForeignKey, Integer, Numeric, String, Text, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped


class PayRunStatus(enum.StrEnum):
    DRAFT = "draft"
    ABA_EXPORTED = "aba_exported"
    FINALIZED = "finalized"


class PayRun(CompanyScoped, Base):
    """Header row for a payroll disbursement run."""

    __tablename__ = "pay_runs"

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
        default=uuid.UUID("00000000-0000-0000-0000-000000000001"),
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PayRunStatus.DRAFT
    )
    journal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("journal_entries.id", ondelete="SET NULL"),
    )
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
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lines: Mapped[list["PayRunLine"]] = relationship(
        back_populates="pay_run",
        cascade="all, delete-orphan",
        order_by="PayRunLine.created_at",
    )


class PayRunLine(Base):
    """One employee line within a PayRun."""

    __tablename__ = "pay_run_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    pay_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pay_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    # FK retarget: from 0112_pay_run_lines_extension onwards this points
    # at employees.id (Phase 1A added the employees table). Old rows
    # were already empty across all 5 live stacks at migration time.
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )
    gross: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    tax: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    super_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    net: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)

    # --- Phase 1B extension (0112_pay_run_lines_extension) ----------------- #
    ordinary_hours: Mapped[Decimal] = mapped_column(
        Numeric(8, 2), nullable=False, default=Decimal("0"), server_default="0"
    )
    overtime_hours: Mapped[Decimal] = mapped_column(
        Numeric(8, 2), nullable=False, default=Decimal("0"), server_default="0"
    )
    leave_hours_paid_annual: Mapped[Decimal] = mapped_column(
        Numeric(8, 2), nullable=False, default=Decimal("0"), server_default="0"
    )
    leave_hours_paid_personal: Mapped[Decimal] = mapped_column(
        Numeric(8, 2), nullable=False, default=Decimal("0"), server_default="0"
    )
    leave_hours_unpaid: Mapped[Decimal] = mapped_column(
        Numeric(8, 2), nullable=False, default=Decimal("0"), server_default="0"
    )
    allowances: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    deductions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    paid_leave_lines: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    lump_sums: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    reportable_fringe_benefits: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    extra_pay: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0"), server_default="0"
    )
    ytd_gross: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0"), server_default="0"
    )
    ytd_tax: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0"), server_default="0"
    )
    ytd_super: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0"), server_default="0"
    )
    payg_scale_used: Mapped[str | None] = mapped_column(String(32))
    payg_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # ---------------------------------------------------------------------- #

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    pay_run: Mapped["PayRun"] = relationship(back_populates="lines")
