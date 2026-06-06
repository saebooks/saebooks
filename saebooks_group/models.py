"""Broker ORM models — pair_registry + relay_log. NO GL tables, ever.

These are the ONLY two tables in the saebooks_group DB. Note what is absent:
no accounts, no journal_entries/lines, no ic_txn/ic_legs, no amounts the broker
can act on. The broker stores PUBLIC keys + token HASHES (never private keys /
cleartext) and routing/audit metadata. The full signed envelope, if retained,
is opaque to the broker (it cannot post it anywhere).
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    TIMESTAMP,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks_group.db import Base


class PairStatus(enum.StrEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class RelayDirection(enum.StrEnum):
    SRC_TO_DST = "SRC_TO_DST"
    DST_TO_SRC = "DST_TO_SRC"


class RelayStatus(enum.StrEnum):
    RECEIVED = "RECEIVED"
    FORWARDED = "FORWARDED"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


class PairRegistry(Base):
    """One registered REMOTE edge: endpoints, PUBLIC keys, token HASHES.

    The broker calls ``dst_endpoint`` (the partner ``/ic/accept``) and verifies
    the originator's signature against ``src_pubkey``. It holds only public keys
    and bcrypt token hashes — a broker compromise yields no signing key, no
    token cleartext, and no money.
    """

    __tablename__ = "pair_registry"

    # Matches the originator tenant's ic_edges.id for traceability.
    edge_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    src_tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    dst_tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    src_endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    dst_endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    src_pubkey: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    dst_pubkey: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # bcrypt hashes of the per-edge tokens the broker presents to each side.
    src_relay_token_hash: Mapped[str | None] = mapped_column(Text)
    dst_relay_token_hash: Mapped[str | None] = mapped_column(Text)
    status: Mapped[PairStatus] = mapped_column(
        String(16), nullable=False, default=PairStatus.PENDING,
        server_default=PairStatus.PENDING.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(),
        nullable=False,
    )


class RelayLog(Base):
    """One brokered delivery — routing + audit only, no money fields.

    ``UNIQUE(edge_id, nonce)`` makes the BROKER itself dedupe/replay-guard, on
    top of the receiver's own guard. ``sig_fingerprint`` is a SHA-256 of the
    signature for audit without storing anything spendable. ``payload_json`` (if
    retained) is the opaque signed envelope for dispute resolution (decision D2).
    """

    __tablename__ = "relay_log"
    __table_args__ = (
        UniqueConstraint("edge_id", "nonce", name="uq_relay_log_edge_nonce"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    ic_txn_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    edge_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pair_registry.edge_id", ondelete="RESTRICT"),
        nullable=False,
    )
    nonce: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    direction: Mapped[RelayDirection] = mapped_column(String(16), nullable=False)
    sig_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[RelayStatus] = mapped_column(
        String(16), nullable=False, default=RelayStatus.RECEIVED,
        server_default=RelayStatus.RECEIVED.value,
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    # Opaque retained envelope (decision D2). The broker cannot act on it.
    payload_json: Mapped[dict | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite")
    )
    received_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    forwarded_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
