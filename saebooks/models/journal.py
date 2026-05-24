import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from saebooks.models.account import Account

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped


class EntryStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    POSTED = "POSTED"
    REVERSED = "REVERSED"


class JournalEntry(CompanyScoped, Base):
    __tablename__ = "journal_entries"
    __table_args__ = (
        UniqueConstraint("company_id", "ref", name="uq_journal_entries_company_ref"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    # tenant_id is required at construction. Phase 1 (0042) shipped a
    # ``00000000`` default for both Python and the DB column so the
    # legacy ``services/journal.py`` could keep working before it
    # accepted tenant_id explicitly. Phase 2 (0127) drops both defaults
    # now that ``create_draft`` raises ``PostingError`` on missing
    # ``tenant_id`` and ``reverse`` inherits from the original entry —
    # the model can stop pretending the legacy dev tenant is a safe
    # fallback, so a bare construct surfaces as an immediate error.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ref: Mapped[str] = mapped_column(String(32), nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[EntryStatus] = mapped_column(
        String(16), nullable=False, default=EntryStatus.DRAFT
    )
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    posted_by: Mapped[str | None] = mapped_column(String)
    reversal_of_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("journal_entries.id", ondelete="SET NULL")
    )
    override_reason: Mapped[str | None] = mapped_column(Text)
    attachments: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lines: Mapped[list["JournalLine"]] = relationship(
        back_populates="entry",
        cascade="all, delete-orphan",
        order_by="JournalLine.line_no",
    )


class JournalLine(Base):
    __tablename__ = "journal_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("journal_entries.id", ondelete="CASCADE"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    description: Mapped[str | None] = mapped_column(Text)
    debit: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    credit: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    tax_code_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_codes.id", ondelete="SET NULL")
    )
    gst_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    # Optional project tag for P&L-by-project reporting. SET NULL on
    # project delete so archiving a project never destroys GL history.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL")
    )
    # Optional department tag for P&L-by-department reporting (FITC-5).
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL")
    )
    # Optional cost-centre tag for P&L-by-cost-centre reporting (FITC-5).
    cost_centre_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cost_centres.id", ondelete="SET NULL")
    )
    # Imputation / franking-credit annotation (PRTR-4). Records the tax
    # offset riding alongside a dividend income line so beneficiary
    # statements can show grossed-up income and imputation credits.
    # Null means no franking dimension — standard non-dividend lines never
    # need to set this.
    franking_credit_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    # Per-line tax-determination snapshot (TaxTreatment.to_jsonable()).
    # Populated by services.journal.post() via the active jurisdictions
    # TaxEngine.compute() so that audit history stays self-consistent
    # even if the underlying tax_code definition changes later.
    # Pre-existing rows from before 0104 stay null — no backfill.
    tax_treatment: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    entry: Mapped[JournalEntry] = relationship(back_populates="lines")
    account: Mapped["Account"] = relationship(
        "Account",
        foreign_keys=[account_id],
        lazy="raise",
    )


class PeriodLock(CompanyScoped, Base):
    __tablename__ = "period_locks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    locked_through: Mapped[date] = mapped_column(Date, nullable=False)
    locked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    locked_by: Mapped[str | None] = mapped_column(String)
    reason: Mapped[str | None] = mapped_column(Text)
