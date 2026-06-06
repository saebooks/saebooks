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
    JSON,
    TIMESTAMP,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
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


class IcEdgeTopology(enum.StrEnum):
    """Whether an edge is same-DB (``LOCAL``) or cross-DB (``REMOTE``).

    Phase 1 edges are all ``LOCAL`` (the migration-0159 column default). A
    ``REMOTE`` edge relays across two tenant DBs via the broker. Stored as a
    plain ``String(16)``.
    """

    LOCAL = "LOCAL"
    REMOTE = "REMOTE"


class IcEdgeRelayStatus(enum.StrEnum):
    """Lifecycle of a REMOTE edge's relay capability.

    A REMOTE edge cannot relay live until ``ACTIVE``, which only the
    accountant-principal authoriser flow (Phase 3c) can set. Stored as a plain
    ``String(16)``.
    """

    INACTIVE = "INACTIVE"
    PENDING_PARTNER = "PENDING_PARTNER"
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class IcOutboxStatus(enum.StrEnum):
    """Dispatcher state machine for an outbound relay message.

    ``PENDING`` (awaiting first send) -> ``SENT`` (broker 2xx) -> ``ACKED``
    (partner accepted); ``FAILED`` (retryable, backoff scheduled) -> ``DEAD``
    (max attempts exhausted; surfaced as an unmatched leg for human action,
    never auto-reversed). Stored as a plain ``String(16)``.
    """

    PENDING = "PENDING"
    SENT = "SENT"
    ACKED = "ACKED"
    FAILED = "FAILED"
    DEAD = "DEAD"


class IcInboxStatus(enum.StrEnum):
    """Lifecycle of a received relay message.

    ``RECEIVED`` (signature + freshness + idempotency passed) -> ``POSTED``
    (reciprocal leg posted) or ``REJECTED`` (verification failed; nothing
    posted, kept for audit). Stored as a plain ``String(16)``.
    """

    RECEIVED = "RECEIVED"
    POSTED = "POSTED"
    REJECTED = "REJECTED"


_STATUS_LEN = 16
_DIRECTION_LEN = 16
_SIDE_LEN = 16
_TOPOLOGY_LEN = 16
_RELAY_STATUS_LEN = 16
_OUTBOX_STATUS_LEN = 16
_INBOX_STATUS_LEN = 16


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
        # NOTE: the relay_contra_account_id composite FK to accounts(id,
        # company_id) is declared in migration 0161 (the authoritative DDL) and
        # NOT mirrored here. A SECOND model-level ForeignKeyConstraint that
        # reuses ``company_id`` to the same target table tripped a metadata
        # FK-resolution error (NoReferencedTableError on accounts.company_id)
        # under full-suite import order. The DB FK still enforces the bound; the
        # ORM does not need the constraint object to map the column.
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
    # Phase 3a (REMOTE): relaxed to nullable — a REMOTE edge's partner lives
    # in a different tenant DB and has no LOCAL companies row. LOCAL edges still
    # carry it (the service/app layer requires it for the LOCAL path).
    partner_company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="RESTRICT"),
        nullable=True,
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
    # ---- Phase 3a REMOTE columns (migration 0159). All nullable/defaulted so
    # existing LOCAL edges are untouched; inert until the relay phases wire them.
    # LOCAL | REMOTE — a REMOTE edge relays across two tenant DBs via the broker.
    topology: Mapped[IcEdgeTopology] = mapped_column(
        String(_TOPOLOGY_LEN),
        nullable=False,
        default=IcEdgeTopology.LOCAL,
        server_default=IcEdgeTopology.LOCAL.value,
    )
    # The partner's tenant id in the partner DB (opaque to us).
    partner_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    # Informational; the real route is via the broker.
    partner_endpoint: Mapped[str | None] = mapped_column(Text)
    # The PARTNER's Ed25519 public key — we verify their inbound legs with this.
    relay_pubkey: Mapped[bytes | None] = mapped_column(LargeBinary)
    # THIS tenant's Ed25519 private key for this edge, Fernet-encrypted
    # (crypto.encrypt_field) — never cleartext, never leaves the tenant.
    relay_privkey_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    # Per-edge scoped bearer (api_token pattern) — prefix lookup + bcrypt hash.
    relay_token_prefix: Mapped[str | None] = mapped_column(String(16))
    relay_token_hash: Mapped[str | None] = mapped_column(Text)
    # INACTIVE | PENDING_PARTNER | ACTIVE | REVOKED. A REMOTE edge cannot relay
    # live until ACTIVE (set only by the accountant-principal authoriser flow).
    relay_status: Mapped[IcEdgeRelayStatus] = mapped_column(
        String(_RELAY_STATUS_LEN),
        nullable=False,
        default=IcEdgeRelayStatus.INACTIVE,
        server_default=IcEdgeRelayStatus.INACTIVE.value,
    )
    # Who turned this edge on (the principal who held grants on both tenants).
    authorised_by_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("principals.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Phase 3c (migration 0161): the REMOTE leg's CONTRA account (this side's
    # bank / clearing). Composite-FK'd to accounts(id, company_id) at the table
    # level so it can only be one of THIS edge's company's own postable accounts
    # — keeping the invariant that NO account id ever crosses the wire (the
    # receiver resolves both control + contra from its OWN edge row). Nullable:
    # LOCAL edges never use it; inert until the relay flag is on.
    relay_contra_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
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


class IcOutbox(CompanyScoped, Base):
    """One ORIGINATED REMOTE event awaiting relay (migration 0159, Phase 3a).

    Written in the SAME local txn as the originator leg (a later phase): the
    local books are never blocked on partner reachability. ``PENDING``/``FAILED``
    rows are the dispatcher's work queue. INERT for now — nothing reads or writes
    this table until the live-relay phase.

    RLS (Class A — direct ``tenant_id``): migration 0159 applies ENABLE + FORCE
    ROW LEVEL SECURITY + the standard ``tenant_isolation`` policy + the 0131
    coherence trigger. The ORM does not add an RLS directive (the migration is
    authoritative DDL). Also ``CompanyScoped`` (app-layer company filter).
    """

    __tablename__ = "ic_outbox"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_ic_outbox_tenant_idempotency_key",
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
    # The shared event id, chosen by the originator and carried in the payload.
    ic_txn_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ic_txn.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Which REMOTE edge this rides (RESTRICT — never orphan an outbox row).
    edge_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ic_edges.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # = ic_txn_id (one outbox row per shared event); UNIQUE per tenant.
    idempotency_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    # Anti-replay material, fresh per message.
    nonce: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # The canonical relay body (see services/ic_relay/signing.canonical_payload).
    payload_json: Mapped[dict[str, object]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=False,
    )
    # Ed25519 detached signature over the canonical bytes.
    signature: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[IcOutboxStatus] = mapped_column(
        String(_OUTBOX_STATUS_LEN),
        nullable=False,
        default=IcOutboxStatus.PENDING,
        server_default=IcOutboxStatus.PENDING.value,
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    issued_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
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


class IcInbox(CompanyScoped, Base):
    """One RECEIVED REMOTE event (migration 0159, Phase 3a).

    ``UNIQUE(tenant_id, ic_txn_id)`` is the idempotency guard (a re-delivered
    message hits the unique violation -> receiver returns the prior ack and
    posts nothing); ``UNIQUE(tenant_id, nonce)`` is the replay guard. INERT for
    now — nothing reads or writes this table until the live-relay phase.

    ``ic_txn_id`` is the ORIGINATOR-chosen external id carried in the payload; it
    is deliberately NOT an FK (the receiver mints its own ``ic_txn`` row when it
    posts the reciprocal leg). RLS + coherence trigger as ``IcOutbox``.
    """

    __tablename__ = "ic_inbox"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "ic_txn_id",
            name="uq_ic_inbox_tenant_ic_txn_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "nonce",
            name="uq_ic_inbox_tenant_nonce",
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
    # The originator-chosen shared id. NOT an FK (the local ic_txn is minted by
    # the receiver). UNIQUE(tenant_id, ic_txn_id) is the idempotency guard.
    ic_txn_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    edge_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ic_edges.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # UNIQUE(tenant_id, nonce) = replay guard.
    nonce: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    payload_json: Mapped[dict[str, object]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=False,
    )
    signature: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # The reciprocal leg once posted (RESTRICT — never deleted out from under
    # the inbox audit row).
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("journal_entries.id", ondelete="RESTRICT"),
        nullable=True,
    )
    status: Mapped[IcInboxStatus] = mapped_column(
        String(_INBOX_STATUS_LEN),
        nullable=False,
        default=IcInboxStatus.RECEIVED,
        server_default=IcInboxStatus.RECEIVED.value,
    )
    reject_reason: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    posted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
