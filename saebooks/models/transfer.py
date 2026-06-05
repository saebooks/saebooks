"""ORM model for the Transfer record type — account-to-account money movement.

A ``Transfer`` is the first-class engine record for moving money between two
balance-sheet accounts of ONE company: bank -> credit-card paydown
(``2-1115``), bank -> director-loan repayment (``2-2200``), bank -> bank /
loan transfers. Before this record type existed these were modelled as
spend-money Expenses coded to a liability account — semantically wrong and
invisible in Payments. A Transfer compiles to exactly ONE balance-sheet
journal entry (Dr to_account / Cr from_account, no GST) via
``services.transfers.create_and_post_transfer``; ``journal_entry_id`` links
back to that posted entry.

Schema materialised by migration ``0155_transfers``.

RLS (Class A — direct ``tenant_id`` column): migration 0155 applies
ENABLE + FORCE ROW LEVEL SECURITY + the standard ``tenant_isolation`` policy
and the 0131 tenant<->company coherence trigger. The migration is the
authoritative DDL; the ORM does not add an RLS directive. The model is
additionally ``CompanyScoped`` (app-layer company filter).

Both ``from_account_id`` and ``to_account_id`` are composite-FK'd to
``accounts(id, company_id)`` (migration 0155, target = the 0152
``uq_accounts_id_company`` unique constraint) so a transfer can never point at
a sister company's account.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped


class TransferStatus(enum.StrEnum):
    """Lifecycle of a transfer.

    Stored as ``String(16)`` on ``Transfer.status`` (mirrors the
    ``EntryStatus``/``IcTxnStatus`` pattern — a Python StrEnum persisted as a
    plain string, no DB enum type).
    """

    POSTED = "POSTED"      # the linked JE is posted
    REVERSED = "REVERSED"  # the linked JE has been reversed


_STATUS_LEN = 16


class Transfer(CompanyScoped, Base):
    """Account-to-account money movement.

    One row per money movement, owned by ``company_id``. The double-entry
    lives on the linked ``JournalEntry`` (``journal_entry_id``); this row is
    the durable, queryable record that gives the movement a stable identity,
    a Payments-surface presence, and directors-loan traceability.
    """

    __tablename__ = "transfers"
    __table_args__ = (
        # from_account_id and to_account_id must each be an account OF
        # company_id. Targets the 0152 uq_accounts_id_company unique
        # constraint so a transfer can never reference a sister company's
        # account. Not declared as single-column FKs to keep the composite
        # constraint authoritative.
        ForeignKeyConstraint(
            ["from_account_id", "company_id"],
            ["accounts.id", "accounts.company_id"],
            name="fk_transfers_from_account_company",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["to_account_id", "company_id"],
            ["accounts.id", "accounts.company_id"],
            name="fk_transfers_to_account_company",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Source account — credited on post (money leaves here). Composite-FK'd
    # above to accounts(id, company_id).
    from_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    # Destination account — debited on post (money arrives here, or a
    # liability is paid down). Composite-FK'd above to accounts(id, company_id).
    to_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
    )
    transfer_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[TransferStatus] = mapped_column(
        String(_STATUS_LEN),
        nullable=False,
        default=TransferStatus.POSTED,
        server_default=TransferStatus.POSTED.value,
    )
    # Linkage to the posted balance-sheet JE this transfer compiled to.
    # RESTRICT (migration 0155) so the JE can never be hard-deleted out from
    # under its transfer; unwind goes through reversal, never delete.
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("journal_entries.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
