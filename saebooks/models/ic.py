"""ORM models for intercompany Phase 1 — ic_txn, ic_edges, ic_legs.

The LOCAL (same-tenant) intercompany foundation. An economic event between two
companies co-resident in one tenant DB is recorded as one ``IcTxn`` (the shared
event) linked to two ``JournalEntry`` rows (one per company) via two ``IcLeg``
rows; the per-company "Due to/from" control account is declared on an
``IcEdge``. Schema materialised by migration ``0154_intercompany_phase1``.

REMOTE (cross-DB) partners — the broker relay, Ed25519 signing, outbox/inbox,
per-edge tokens — are Phase 3 and NOT modelled here. The seam lives in
``services/intercompany.py`` (see its ``TODO(remote-relay)``).

RLS (Class A — direct ``tenant_id`` column): migration 0154 applies
ENABLE + FORCE ROW LEVEL SECURITY + the standard ``tenant_isolation`` policy and
the 0131 tenant<->company coherence trigger to all three tables. The migration
is the authoritative DDL; the ORM does not add an RLS directive. All three are
additionally ``CompanyScoped`` (app-layer company filter via
``services.tenant._scope_guard``).
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped


class IcTxnStatus(enum.StrEnum):
    """Lifecycle of a shared intercompany transaction.

    Stored as ``String(16)`` on ``IcTxn.status`` (mirrors the
    ``EntryStatus``/``String(16)`` pattern — a Python StrEnum persisted as a
    plain string, no DB enum type).
    """

    ACTIVE = "ACTIVE"      # both legs posted
    SETTLED = "SETTLED"    # settled (reciprocal repayment posted)
    REVERSED = "REVERSED"  # both legs reversed


class IcEdgeDirection(enum.StrEnum):
    """Which end of a reciprocal intercompany edge a company plays.

    A bidirectional edge between companies A and B is two ``IcEdge`` rows: A's
    row is ``ORIGINATOR``, B's row is ``COUNTERPARTY`` (and vice-versa for the
    return direction). Stored as a plain ``String(16)``.
    """

    ORIGINATOR = "ORIGINATOR"
    COUNTERPARTY = "COUNTERPARTY"


class IcLegSide(enum.StrEnum):
    """Which side of a shared ``IcTxn`` a leg posts.

    The originating company's leg is ``ORIGINATOR``; the partner company's
    mirror leg is ``COUNTERPARTY``. Stored as a plain ``String(16)``.
    """

    ORIGINATOR = "ORIGINATOR"
    COUNTERPARTY = "COUNTERPARTY"


_STATUS_LEN = 16
_DIRECTION_LEN = 16
_SIDE_LEN = 16


class IcTxn(CompanyScoped, Base):
    """The shared intercompany economic event.

    One row per linked reciprocal pair, owned by the originating company. The
    two legs (one ``JournalEntry`` per company) point back at this row via
    ``IcLeg.ic_txn_id``.
    """

    __tablename__ = "ic_txn"

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
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[IcTxnStatus] = mapped_column(
        String(_STATUS_LEN),
        nullable=False,
        default=IcTxnStatus.ACTIVE,
        server_default=IcTxnStatus.ACTIVE.value,
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


class IcEdge(CompanyScoped, Base):
    """The partner relationship = the intercompany capability.

    Reciprocity is a matching row in the partner company (an ``ORIGINATOR`` row
    on one side, a ``COUNTERPARTY`` row on the other). ``control_account_id`` is
    the balance-sheet "Due to/from" control account on THIS company's CoA;
    migration 0154 composite-FKs it to ``accounts(id, company_id)`` so it can
    never reference a sister company's account.

    Phase 1 (LOCAL) requires ``partner_company_id`` (same-tenant companies FK).
    Phase 3 (REMOTE) will add a nullable ``partner_member_id`` for cross-DB
    partners and relax that NOT NULL — see ``services/intercompany.py``.
    """

    __tablename__ = "ic_edges"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "partner_company_id",
            "direction",
            name="uq_ic_edges_company_partner_direction",
        ),
        ForeignKeyConstraint(
            ["control_account_id", "company_id"],
            ["accounts.id", "accounts.company_id"],
            name="fk_ic_edges_control_account_company",
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
    partner_company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Composite-FK'd to accounts(id, company_id) at the table level (above);
    # not declared as a single-column FK here to keep the composite constraint
    # the authoritative one.
    control_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    direction: Mapped[IcEdgeDirection] = mapped_column(
        String(_DIRECTION_LEN),
        nullable=False,
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


class IcLeg(CompanyScoped, Base):
    """Links one local ``JournalEntry`` to a shared ``IcTxn``.

    ``side`` records whether this leg is the originating company's
    (``ORIGINATOR``) or the partner's mirror (``COUNTERPARTY``).
    ``journal_entry_id`` is RESTRICT-deleted (migration 0154) so a posted leg
    can never be hard-deleted out from under its pair; intercompany unwind goes
    through reversal, never delete.
    """

    __tablename__ = "ic_legs"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "ic_txn_id",
            "side",
            name="uq_ic_legs_company_txn_side",
        ),
        UniqueConstraint(
            "company_id",
            "journal_entry_id",
            name="uq_ic_legs_company_journal_entry",
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
    ic_txn_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ic_txn.id", ondelete="CASCADE"),
        nullable=False,
    )
    journal_entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("journal_entries.id", ondelete="RESTRICT"),
        nullable=False,
    )
    side: Mapped[IcLegSide] = mapped_column(
        String(_SIDE_LEN),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
