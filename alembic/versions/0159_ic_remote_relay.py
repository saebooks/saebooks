"""Intercompany Phase 3a — REMOTE relay foundation (ic_outbox / ic_inbox + edges).

SHIP-SAFE, INERT. This migration adds the durable substrate for the cross-DB
intercompany REMOTE relay (the broker / Ed25519 signing / dispatcher land in
later phases 3b/3c). **Nothing reads or writes these objects yet** — the LOCAL
``post_local_pair`` path (migration 0154 + ``services/intercompany.py``) is
untouched. It is therefore a pure additive migration that is safe to apply to
every tenant stack ahead of the live relay.

What it adds
------------
* ``ic_outbox`` — one row per *originated* REMOTE event awaiting relay. Carries
  the shared ``ic_txn_id`` (chosen by the originator), the signed canonical
  payload, the detached Ed25519 ``signature``, and a small dispatcher state
  machine (``status`` / ``attempts`` / ``next_attempt_at`` / ``last_error``).
* ``ic_inbox`` — one row per *received* REMOTE event. The
  ``UNIQUE(tenant_id, ic_txn_id)`` is the idempotency guard (a re-delivered
  message hits the unique violation → the receiver returns the prior ack and
  posts nothing); the ``UNIQUE(tenant_id, nonce)`` is the replay guard.
* REMOTE columns on ``ic_edges`` (relaxing the LOCAL-only shape the 0154 model
  forward-noted): ``topology``, ``partner_tenant_id``, ``partner_endpoint``,
  ``relay_pubkey`` (partner's verify key), ``relay_privkey_ciphertext`` (this
  tenant's Fernet-wrapped signing key — never cleartext, never leaves the
  tenant), per-edge ``relay_token_prefix`` / ``relay_token_hash``,
  ``relay_status``, ``authorised_by_principal_id``; and ``partner_company_id``
  is relaxed to NULL (a REMOTE edge has no *local* partner company).

Both new tables follow the non-negotiable new-table RLS checklist (the verbatim
0154/0156 shape):

  * ``tenant_id`` NOT NULL + FK ``tenants`` (RESTRICT) and ``company_id`` NOT
    NULL + FK ``companies`` (CASCADE).
  * ``ENABLE`` + ``FORCE`` ROW LEVEL SECURITY + the standard ``tenant_isolation``
    policy (the 0055/0150/0154 ``app.current_tenant`` predicate, USING + WITH
    CHECK).
  * The 0131/0152/0154 ``assert_child_tenant_matches_company`` coherence trigger
    so ``tenant_id`` can never disagree with ``companies.tenant_id`` for the
    row's ``company_id``.
  * Explicit ``GRANT … TO saebooks_app`` per table (default privileges silently
    miss tables created under the non-owner migration role — 0138/0152/0154
    precedent).
  * Every unique constraint LEADS with ``tenant_id`` so a constraint-violation
    error can't enumerate another tenant's rows.

SQLite (Cashbook) has no RLS / GUC: the tables/columns are still created for
ORM/schema parity but the RLS + coherence-trigger machinery is a Postgres-only
no-op (mirror 0156's ``if not _is_postgres(): return`` guard for the privileged
parts).

Reversible: ``downgrade`` drops the two tables and their policies/triggers in
FK-safe order and removes the ``ic_edges`` REMOTE columns, restoring
``partner_company_id`` NOT NULL (safe — no NULL rows can exist while inert).

Revision ID: 0159_ic_remote_relay
Revises:     0158_reclassifications
Create Date: 2026-06-06
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0159_ic_remote_relay"
down_revision: str | None = "0158_reclassifications"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Reuse the one-policy-shape-for-the-whole-DB predicate (0055/0150/0154 verbatim).
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
_WITH_CHECK = _USING

# Create parents first; drop is reversed.
_NEW_TABLES = ("ic_outbox", "ic_inbox")

# New REMOTE columns on ic_edges (drop order is reverse of this on downgrade).
_EDGE_COLUMNS = (
    "topology",
    "partner_tenant_id",
    "partner_endpoint",
    "relay_pubkey",
    "relay_privkey_ciphertext",
    "relay_token_prefix",
    "relay_token_hash",
    "relay_status",
    "authorised_by_principal_id",
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _apply_rls(table: str) -> None:
    op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
    op.execute(
        sa.text(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"FOR ALL USING {_USING} WITH CHECK {_WITH_CHECK}"
        )
    )


def _apply_coherence_trigger(table: str) -> None:
    # Reuse the existing 0131/0152/0154 assert_child_tenant_matches_company()
    # function — every row's tenant_id must equal companies.tenant_id for its
    # company_id. The function already exists; we only attach a per-table trigger.
    trg = f"trg_{table}_tenant_coherence"
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trg} ON {table}"))
    op.execute(
        sa.text(
            f"CREATE TRIGGER {trg} "
            f"BEFORE INSERT OR UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION assert_child_tenant_matches_company()"
        )
    )


def _grant_app(table: str) -> None:
    op.execute(
        sa.text(
            f"""
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'saebooks_app') THEN
                    GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO saebooks_app;
                END IF;
            END $$;
            """
        )
    )


def upgrade() -> None:
    is_pg = _is_postgres()

    # ----------------------------------------------------------------- ic_outbox
    # One row per ORIGINATED remote event awaiting relay. PENDING/FAILED rows
    # are the dispatcher's work queue (a later phase); inert for now.
    op.create_table(
        "ic_outbox",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()") if is_pg else None,
            nullable=False,
            primary_key=True,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The shared event id, chosen by the originator and carried in the
        # payload so both DBs key the pair on the same value.
        sa.Column(
            "ic_txn_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ic_txn.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Which REMOTE edge this rides. RESTRICT: never orphan an outbox row by
        # deleting its edge out from under it.
        sa.Column(
            "edge_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ic_edges.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # = ic_txn_id (one outbox row per shared event); UNIQUE per tenant.
        sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=False),
        # Anti-replay material, fresh per message.
        sa.Column("nonce", postgresql.UUID(as_uuid=True), nullable=False),
        # The canonical relay body (see services/ic_relay/signing.canonical_payload).
        sa.Column(
            "payload_json",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
        ),
        # Ed25519 detached signature over the canonical bytes.
        sa.Column("signature", sa.LargeBinary(), nullable=False),
        # PENDING | SENT | ACKED | FAILED | DEAD. Plain String, enum in Python.
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("next_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "issued_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()") if is_pg else sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()") if is_pg else sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()") if is_pg else sa.func.now(),
            nullable=False,
        ),
        # One outbox row per shared event, per tenant. Leads with tenant_id.
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_ic_outbox_tenant_idempotency_key",
        ),
    )
    op.create_index("ix_ic_outbox_tenant_id", "ic_outbox", ["tenant_id"])
    op.create_index("ix_ic_outbox_company_id", "ic_outbox", ["company_id"])
    op.create_index("ix_ic_outbox_ic_txn_id", "ic_outbox", ["ic_txn_id"])
    op.create_index("ix_ic_outbox_edge_id", "ic_outbox", ["edge_id"])
    # Hot poll for the (future) dispatcher: pending/failed rows by schedule.
    if is_pg:
        op.create_index(
            "ix_ic_outbox_dispatch_due",
            "ic_outbox",
            ["next_attempt_at"],
            postgresql_where=sa.text("status IN ('PENDING','FAILED')"),
        )

    # ------------------------------------------------------------------ ic_inbox
    # One row per RECEIVED remote event. The two unique constraints are the
    # idempotency (ic_txn_id) and replay (nonce) guards.
    op.create_table(
        "ic_inbox",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()") if is_pg else None,
            nullable=False,
            primary_key=True,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The originator-chosen shared id. UNIQUE(tenant_id, ic_txn_id) is the
        # idempotency guard. NOT an FK: the ic_txn is minted locally by the
        # receiver when it posts, so this column is the carried external id.
        sa.Column("ic_txn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "edge_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ic_edges.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # UNIQUE(tenant_id, nonce) = replay guard.
        sa.Column("nonce", postgresql.UUID(as_uuid=True), nullable=False),
        # The received body, retained for audit / dispute.
        sa.Column(
            "payload_json",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
        ),
        # The verified signature, kept for non-repudiation.
        sa.Column("signature", sa.LargeBinary(), nullable=False),
        # The reciprocal leg once posted (RESTRICT so it can't be deleted out
        # from under the inbox audit row).
        sa.Column(
            "journal_entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("journal_entries.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        # RECEIVED | POSTED | REJECTED. Plain String, enum in Python.
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="RECEIVED",
            nullable=False,
        ),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()") if is_pg else sa.func.now(),
            nullable=False,
        ),
        sa.Column("posted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        # Idempotency guard: a re-delivered message hits this unique violation.
        sa.UniqueConstraint(
            "tenant_id",
            "ic_txn_id",
            name="uq_ic_inbox_tenant_ic_txn_id",
        ),
        # Replay guard: a replayed nonce hits this unique violation.
        sa.UniqueConstraint(
            "tenant_id",
            "nonce",
            name="uq_ic_inbox_tenant_nonce",
        ),
    )
    op.create_index("ix_ic_inbox_tenant_id", "ic_inbox", ["tenant_id"])
    op.create_index("ix_ic_inbox_company_id", "ic_inbox", ["company_id"])
    op.create_index("ix_ic_inbox_edge_id", "ic_inbox", ["edge_id"])

    # --------------------------------------------------- ic_edges REMOTE columns
    # All nullable / defaulted so existing LOCAL edges are untouched.
    op.add_column(
        "ic_edges",
        sa.Column(
            "topology",
            sa.String(length=16),
            server_default="LOCAL",
            nullable=False,
        ),
    )
    op.add_column(
        "ic_edges",
        sa.Column("partner_tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "ic_edges",
        sa.Column("partner_endpoint", sa.Text(), nullable=True),
    )
    op.add_column(
        "ic_edges",
        sa.Column("relay_pubkey", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "ic_edges",
        sa.Column("relay_privkey_ciphertext", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "ic_edges",
        sa.Column("relay_token_prefix", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "ic_edges",
        sa.Column("relay_token_hash", sa.Text(), nullable=True),
    )
    op.add_column(
        "ic_edges",
        sa.Column(
            "relay_status",
            sa.String(length=16),
            server_default="INACTIVE",
            nullable=False,
        ),
    )
    op.add_column(
        "ic_edges",
        sa.Column(
            "authorised_by_principal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("principals.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    # A REMOTE edge has no LOCAL partner company — relax the 0154 NOT NULL.
    op.alter_column("ic_edges", "partner_company_id", nullable=True)

    if not is_pg:
        # SQLite Cashbook: no RLS / GUC / coherence trigger. The tables exist
        # for ORM/schema parity; isolation is application-layer single-tenant.
        return

    for t in _NEW_TABLES:
        _apply_rls(t)
        _apply_coherence_trigger(t)
        _grant_app(t)


def downgrade() -> None:
    is_pg = _is_postgres()

    # Restore the LOCAL NOT NULL on partner_company_id first (safe: while the
    # relay is inert no REMOTE edge with a NULL partner_company_id can exist).
    op.alter_column("ic_edges", "partner_company_id", nullable=False)
    for col in reversed(_EDGE_COLUMNS):
        op.drop_column("ic_edges", col)

    if is_pg:
        for t in reversed(_NEW_TABLES):
            op.execute(
                sa.text(f"DROP TRIGGER IF EXISTS trg_{t}_tenant_coherence ON {t}")
            )
            op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {t}"))
            op.execute(sa.text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY"))

    op.drop_table("ic_inbox")
    op.drop_table("ic_outbox")
    # NOTE: do NOT drop assert_child_tenant_matches_company() — it is owned by
    # 0131 and many other triggers depend on it.
