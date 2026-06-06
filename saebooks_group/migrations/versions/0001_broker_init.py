"""Broker init — pair_registry + relay_log (saebooks_group). NO GL tables.

The entire broker schema, created from empty. Two tables only:
  * pair_registry — registered REMOTE edges: endpoints, PUBLIC keys, token
    HASHES, status. No private keys, no cleartext, no money.
  * relay_log — delivery audit: routing metadata + signature fingerprint +
    status machine. UNIQUE(edge_id, nonce) so the broker also dedupes/replay-
    guards. No amount/account columns the broker can act on.

The "broker never holds money" invariant is asserted by
tests/test_broker_money_free.py (schema has none of the GL table names; broker
code imports no posting service).

Revision ID: 0001_broker_init
Revises:
Create Date: 2026-06-06
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_broker_init"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pair_registry",
        sa.Column("edge_id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True),
        sa.Column("src_tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dst_tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("src_endpoint", sa.Text(), nullable=False),
        sa.Column("dst_endpoint", sa.Text(), nullable=False),
        sa.Column("src_pubkey", sa.LargeBinary(), nullable=False),
        sa.Column("dst_pubkey", sa.LargeBinary(), nullable=False),
        sa.Column("src_relay_token_hash", sa.Text(), nullable=True),
        sa.Column("dst_relay_token_hash", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="PENDING", nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_pair_registry_src_tenant", "pair_registry", ["src_tenant_id"])
    op.create_index("ix_pair_registry_dst_tenant", "pair_registry", ["dst_tenant_id"])

    op.create_table(
        "relay_log",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"), nullable=False, primary_key=True,
        ),
        sa.Column("ic_txn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "edge_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pair_registry.edge_id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("nonce", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("sig_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="RECEIVED", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("payload_json", postgresql.JSONB(), nullable=True),
        sa.Column("received_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("forwarded_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        # The broker also dedupes/replay-guards on (edge_id, nonce).
        sa.UniqueConstraint("edge_id", "nonce", name="uq_relay_log_edge_nonce"),
    )
    op.create_index("ix_relay_log_ic_txn_id", "relay_log", ["ic_txn_id"])
    op.create_index("ix_relay_log_edge_id", "relay_log", ["edge_id"])


def downgrade() -> None:
    op.drop_table("relay_log")
    op.drop_table("pair_registry")
