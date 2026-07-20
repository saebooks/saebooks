"""TimeEntry — hours logged against a project / user / contact.

Standalone v1, sibling to the future ``Employee`` model. A time entry
represents N hours of work on a given date by ``user_id`` (always set)
optionally on behalf of a ``contact_id`` (when the worker is a tracked
contractor rather than the logged-in user themselves).

Billable entries can flow to an invoice line via the
``services.time_entries.convert_to_invoice_line`` service; once
converted, ``invoice_line_id`` is set and the entry is frozen.

Approval workflow:
    DRAFT --submit--> SUBMITTED --approve--> APPROVED --lock--> LOCKED
                              \\--reject--> REJECTED

DRAFT entries are editable; APPROVED entries are frozen except by an
admin; LOCKED entries belong to a finalised pay run and can never be
edited (only voided + replaced).
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    Time,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped
from saebooks.money import money_quantum


class TimeEntryApprovalStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    LOCKED = "LOCKED"


class TimeEntry(CompanyScoped, Base):
    __tablename__ = "time_entries"
    __table_args__ = (
        CheckConstraint("hours > 0", name="ck_time_entries_hours_positive"),
        CheckConstraint(
            "(start_time IS NULL) = (end_time IS NULL)",
            name="ck_time_entries_clock_pair",
        ),
        CheckConstraint(
            "break_minutes >= 0", name="ck_time_entries_break_nonneg"
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
        default=uuid.UUID("00000000-0000-0000-0000-000000000001"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="RESTRICT"),
    )
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    hours: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    end_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    break_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("departments.id", ondelete="SET NULL"),
    )
    cost_centre_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("cost_centres.id", ondelete="SET NULL"),
    )
    billable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    invoice_line_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoice_lines.id", ondelete="SET NULL"),
    )
    approval_status: Mapped[str] = mapped_column(
        Enum(
            *[s.value for s in TimeEntryApprovalStatus],
            name="time_entry_approval_status_enum",
            create_type=False,
        ),
        nullable=False,
        default=TimeEntryApprovalStatus.DRAFT.value,
        server_default=TimeEntryApprovalStatus.DRAFT.value,
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    @property
    def line_total(self) -> Decimal:
        """Convenience: rate × hours, used by the convert-to-invoice flow.

        Returns Decimal(0) when rate is unset — the conversion service
        falls back to the project / contact default rate at that point.
        """
        if self.rate is None:
            return Decimal("0")
        return (self.rate * self.hours).quantize(money_quantum(2))
