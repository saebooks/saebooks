"""Bank statement lines for reconciliation."""
import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base


class StatementLineStatus(enum.StrEnum):
    UNMATCHED = "UNMATCHED"
    MATCHED = "MATCHED"


class BankStatementLine(Base):
    __tablename__ = "bank_statement_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False,
        comment="The bank/cash account this line belongs to",
    )
    txn_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False,
        comment="Positive=deposit, negative=withdrawal",
    )
    reference: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[StatementLineStatus] = mapped_column(
        String(16), nullable=False, default=StatementLineStatus.UNMATCHED
    )
    matched_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("journal_entries.id", ondelete="SET NULL"),
        comment="The journal entry this line was reconciled against",
    )
    matched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    matched_by: Mapped[str | None] = mapped_column(String)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL"),
        comment="Optional contact (customer/supplier) inferred from rule or manual",
    )
    bank_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bank_rules.id", ondelete="SET NULL"),
        comment="The bank rule used to auto-match this line, if any",
    )
    bank_feed_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bank_feed_accounts.id", ondelete="SET NULL"),
        comment="Source feed account, if this line came from a bank feed",
    )
    external_id: Mapped[str | None] = mapped_column(
        String(255),
        comment="Upstream transactionId — used to dedupe on resync",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
