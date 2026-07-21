"""Bank statement lines for reconciliation."""
import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped

_DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"


class StatementLineStatus(enum.StrEnum):
    UNMATCHED = "UNMATCHED"
    PARTIAL = "PARTIAL"
    MATCHED = "MATCHED"
    IGNORED = "IGNORED"


class BankStatementLine(CompanyScoped, Base):
    __tablename__ = "bank_statement_lines"
    __table_args__ = (
        # Partial unique index — feed-ingested lines are deduped on
        # (bank_feed_account_id, external_id); manually-entered lines
        # (external_id IS NULL) stay unconstrained. This is the conflict
        # target for the feed bulk upsert in services.statement_lines_bulk.
        # It was created only by Postgres migration 0016 (raw op.create_index),
        # so SQLite's bootstrap_schema never had it and the on_conflict upsert
        # would fail there. Declaring it here creates it on SQLite bootstrap
        # too (sqlite_where) and keeps the ORM in lock-step with Postgres
        # (postgresql_where mirrors 0016 exactly — no new migration).
        Index(
            "ux_bank_statement_lines_feed_external",
            "bank_feed_account_id",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
            sqlite_where=text("external_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        server_default=_DEFAULT_TENANT_ID,
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
    balance: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2),
        comment="Running balance after this line, if captured",
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
    matched_to_type: Mapped[str | None] = mapped_column(
        String(32),
        comment="PAYMENT or JOURNAL_ENTRY — type of the matched transaction",
    )
    matched_to_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        comment="UUID of the matched payment or journal entry (no FK constraint)",
    )
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
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
