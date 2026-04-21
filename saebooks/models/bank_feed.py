"""Bank-feed link tables.

These three tables sit between saebooks' ``companies`` / ``accounts``
rows and the upstream aggregator's client / account / feed-issue
identifiers. The business logic that reads/writes them lives in
``saebooks.services.bank_feeds``.

Design notes:

- ``BankFeedClient`` is 1:1 with ``Company`` — one company maps to one
  aggregator-side client identifier.
- ``BankFeedAccount`` is N:1 under a client; each row pairs an
  aggregator account with the chart-of-accounts row that statement
  lines post to.
- ``BankFeedIssue`` is an opportunistic cache of the aggregator's
  feed-health feed — keyed on the upstream issue id so repeated
  fetches are idempotent.

No secrets are stored here. OAuth client id/secret + APIM subscription
key live in ``saebooks.config.Settings`` (per-install env vars).
"""
import enum
import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped


class BankFeedIssueStatus(enum.StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


class BankFeedClient(CompanyScoped, Base):
    """One row per ``Company`` that has been registered with the aggregator."""

    __tablename__ = "bank_feed_clients"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    sds_client_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class BankFeedAccount(CompanyScoped, Base):
    """One row per aggregator-side account connected to a SAE Books ledger."""

    __tablename__ = "bank_feed_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    bank_feed_client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bank_feed_clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    ledger_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sds_account_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    sds_institution_id: Mapped[str] = mapped_column(String(128), nullable=False)
    masked_number: Mapped[str | None] = mapped_column(String(64))
    display_name: Mapped[str | None] = mapped_column(String(255))
    product_category: Mapped[str | None] = mapped_column(String(64))
    feed_type: Mapped[str | None] = mapped_column(String(32))
    processing_status: Mapped[str | None] = mapped_column(String(4))
    processing_status_date: Mapped[date | None] = mapped_column(Date)
    last_transaction_posted_id: Mapped[str | None] = mapped_column(String(128))
    last_transaction_posted_date: Mapped[date | None] = mapped_column(Date)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class BankFeedIssue(Base):
    """Cache of ``GET /feedissues`` responses for the admin dashboard."""

    __tablename__ = "bank_feed_issues"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    sds_feed_issue_id: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True
    )
    sds_institution_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[BankFeedIssueStatus] = mapped_column(String(16), nullable=False)
    creation_datetime: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    closed_datetime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message: Mapped[str | None] = mapped_column(Text)
    last_update_datetime: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    country: Mapped[str | None] = mapped_column(String(8))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
