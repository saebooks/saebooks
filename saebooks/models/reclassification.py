"""ORM model for the Reclassification record type — account-to-account
classification move of an already-posted amount, WITHOUT mutating the
original posted entry.

Why this record exists (Gap 2, see ``saebooks-0157-builder-prompt.md``)
-----------------------------------------------------------------------
The 0156 ledger-cleanup re-points ~983 posted expenses into new child
accounts. The only engine-clean way to do that today is void+recreate —
heavy, and it leaves void clutter for what is a *pure classification
change*. A ``Reclassification`` is the lightweight, audit-preserving
alternative: it moves ``amount`` from ``from_account`` to ``to_account`` by
posting ONE balanced, engine-generated **reclass JE** through the posting
chokepoint, and it leaves the ORIGINAL posted entry completely untouched.

What a reclassification is
--------------------------
A ``Reclassification`` row records the move; one ``JournalEntry`` (two lines)
records the double-entry. The JE is stamped ``origin=RECLASSIFICATION``,
``source_type='reclassification'``, ``source_id=<reclassification.id>``, and
the row's ``journal_entry_id`` points back at the posted entry.

Sign convention — the reclass nets the OLD account to zero and moves the
amount to the NEW account. The direction follows the natural balance side of
the pair (both accounts MUST be the same natural side — see
``services/reclassifications.py``):

* **Debit-natured** pair (ASSET / EXPENSE / COST_OF_SALES / OTHER_EXPENSE):
  the JE is **Dr to_account / Cr from_account**. ``Cr from`` nets the old
  debit-natured account toward zero; ``Dr to`` lands the amount on the new
  account. This is the primary case — the ~983 posted expenses moving into
  child expense accounts (e.g. ``6-1000`` -> ``6-1010``).
* **Credit-natured** pair (LIABILITY / EQUITY / INCOME / OTHER_INCOME): the
  mirror, **Dr from_account / Cr to_account**, so the old credit-natured
  account still nets toward zero.

``source_entry_id`` (nullable) is the originating JE being reclassified, for
traceability only — it is NOT mutated. ``journal_entry_id`` is the generated
reclass JE.

Schema materialised by migration ``0158_reclassifications``.

RLS (Class A — direct ``tenant_id`` column): migration 0158 applies
ENABLE + FORCE ROW LEVEL SECURITY + the standard ``tenant_isolation`` policy
and the 0131 tenant<->company coherence trigger. The migration is the
authoritative DDL; the ORM does not add an RLS directive. The model is
additionally ``CompanyScoped`` (app-layer company filter).

Both ``from_account_id`` and ``to_account_id`` are composite-FK'd to
``accounts(id, company_id)`` (migration 0158, target = the 0152
``uq_accounts_id_company`` unique constraint) so a reclassification can never
point at a sister company's account.
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


class ReclassificationStatus(enum.StrEnum):
    """Lifecycle of a reclassification.

    Stored as ``String(16)`` on ``Reclassification.status`` (mirrors the
    ``EntryStatus``/``TransferStatus`` pattern — a Python StrEnum persisted as
    a plain string, no DB enum type).
    """

    POSTED = "POSTED"      # the reclass JE is posted
    REVERSED = "REVERSED"  # the reclass JE has been reversed


_STATUS_LEN = 16


class Reclassification(CompanyScoped, Base):
    """Account-to-account classification move of an already-posted amount.

    One row per move, owned by ``company_id``. The double-entry lives on the
    linked reclass ``JournalEntry`` (``journal_entry_id``); this row is the
    durable, queryable record that gives the move a stable identity and links
    it to the original entry it reclassifies (``source_entry_id``) for audit.
    The original posted entry is never mutated.
    """

    __tablename__ = "reclassifications"
    __table_args__ = (
        # from_account_id and to_account_id must each be an account OF
        # company_id. Targets the 0152 uq_accounts_id_company unique
        # constraint so a reclassification can never reference a sister
        # company's account. Not declared as single-column FKs to keep the
        # composite constraint authoritative.
        ForeignKeyConstraint(
            ["from_account_id", "company_id"],
            ["accounts.id", "accounts.company_id"],
            name="fk_reclassifications_from_account_company",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["to_account_id", "company_id"],
            ["accounts.id", "accounts.company_id"],
            name="fk_reclassifications_to_account_company",
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
    # The account the amount is moving OUT of (the misclassification).
    # Composite-FK'd above to accounts(id, company_id).
    from_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    # The account the amount is moving INTO (the correct classification,
    # typically a child account). Composite-FK'd above to accounts(id,
    # company_id).
    to_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
    )
    reclass_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    # The original JE being reclassified — traceability ONLY. SET NULL so
    # archiving/hard-deleting the source entry never destroys the move's
    # provenance row, and so this column can never block a delete the way a
    # RESTRICT would. The source entry is NOT mutated by the reclass.
    source_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("journal_entries.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Linkage to the posted reclass JE this move compiled to. RESTRICT
    # (migration 0158) so the JE can never be hard-deleted out from under its
    # reclassification; unwind goes through reversal, never delete.
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("journal_entries.id", ondelete="RESTRICT"),
        nullable=True,
    )
    status: Mapped[ReclassificationStatus] = mapped_column(
        String(_STATUS_LEN),
        nullable=False,
        default=ReclassificationStatus.POSTED,
        server_default=ReclassificationStatus.POSTED.value,
    )
    created_by: Mapped[str | None] = mapped_column(String)
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
