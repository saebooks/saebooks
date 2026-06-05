"""Intercompany Phase 1 — ic_txn, ic_edges, ic_legs (LOCAL same-tenant pairs).

Phase 1 of the intercompany / group-ledger design
(``saebooks-intercompany-accountant-design.md`` + the 2026-06-02 group-ledger
spec). Builds the **mergeable foundation** for posting a linked reciprocal pair
of journal entries between two companies **co-resident in one tenant DB** (the
LOCAL fast-path). The REMOTE cross-DB relay (broker, Ed25519 signing,
outbox/inbox, per-edge tokens) is Phase 3 and is explicitly NOT built here — the
seam is documented in ``services/intercompany.py``.

Three tenant-scoped tables, each following the non-negotiable new-table RLS
checklist used by 0150/0133/0152:

  * ``tenant_id`` NOT NULL + FK ``tenants`` (RESTRICT — never let a tenant
    delete out from under its IC history) and ``company_id`` NOT NULL + FK
    ``companies`` (CASCADE).
  * ``ENABLE`` + ``FORCE`` ROW LEVEL SECURITY + the standard ``tenant_isolation``
    policy (the verbatim 0055/0088/0150 ``app.current_tenant`` predicate, with
    the ``, true`` sentinel and a ``WITH CHECK`` clause).
  * The 0131/0152 ``assert_child_tenant_matches_company`` coherence trigger so
    ``tenant_id`` can never disagree with ``companies.tenant_id`` for the row's
    ``company_id`` (defends the same-tenant wrong-company case).
  * Composite ``(account_id, company_id) -> accounts(id, company_id)`` FKs on
    every account-referencing column (``ic_edges.control_account_id``) so the DB
    itself refuses a leg/edge that points at a sister company's account — the
    0152 ``uq_accounts_id_company`` target already exists.
  * Explicit ``GRANT … TO saebooks_app`` per table (default privileges silently
    miss tables created under the non-owner migration role — 0138/0152
    precedent).
  * All unique constraints LEAD with ``company_id`` so a constraint-violation
    error can't enumerate sister-company rows.

Tables:

``ic_txn`` — the shared intercompany economic event. One row per linked pair,
owned by the originating company. (id, tenant_id, company_id, description,
status, created_at, updated_at). ``status`` is a plain ``String(16)`` mirroring
the ``EntryStatus`` pattern: ``ACTIVE`` (legs posted) / ``SETTLED`` /
``REVERSED``.

``ic_edges`` — the partner relationship = the capability. Reciprocity is a
matching row in each side's company (and, in Phase 3, each side's DB).
(company_id, partner_company_id, control_account_id, direction). ``direction``
records whether this company is the ``ORIGINATOR`` or ``COUNTERPARTY`` end (a
reciprocal pair has one of each). ``partner_company_id`` is a same-tenant
companies FK for the LOCAL case (Phase 3 adds a nullable ``partner_member_id``
for REMOTE — out of scope here).

``ic_legs`` — links one local ``journal_entries`` row to an ``ic_txn``.
(ic_txn_id, journal_entry_id, company_id, side). ``side`` = ``ORIGINATOR`` /
``COUNTERPARTY``. ``journal_entry_id`` FK is ``ondelete=RESTRICT`` so a posted
leg can never be hard-deleted out from under its pair (the 0152 journal.delete
guard already refuses to delete an IC-linked posted JE).

Reversible: ``downgrade`` drops the three tables and their triggers/policies in
FK-safe order.

Revision ID: 0154_intercompany_phase1
Revises:     0153_je_provenance
Create Date: 2026-06-06
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0154_intercompany_phase1"
down_revision: str | None = "0153_je_provenance"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Reuse the one-policy-shape-for-the-whole-DB predicate (0055/0088/0150 verbatim).
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
_WITH_CHECK = _USING

# Order matters for create (parents first) and is reversed for drop.
_TABLES = ("ic_txn", "ic_edges", "ic_legs")


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
    # Reuse the existing 0131/0152 assert_child_tenant_matches_company()
    # function — every row's tenant_id must equal companies.tenant_id for its
    # company_id. CREATE OR REPLACE is NOT needed (the function already exists
    # from 0131); we only attach a per-table trigger.
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
    # ------------------------------------------------------------------ ic_txn
    # The shared intercompany economic event. One row per linked reciprocal
    # pair, owned by the originating company. Settlement/reversal flip status.
    op.create_table(
        "ic_txn",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
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
        sa.Column("description", sa.Text(), nullable=True),
        # ACTIVE (legs posted) / SETTLED / REVERSED. Plain String, StrEnum
        # enforced in Python (mirrors EntryStatus / 0153 origin pattern).
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="ACTIVE",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_ic_txn_tenant_id", "ic_txn", ["tenant_id"])
    op.create_index("ix_ic_txn_company_id", "ic_txn", ["company_id"])

    # ----------------------------------------------------------------- ic_edges
    # The partner relationship = the capability. Reciprocity = a matching row
    # in the partner company (one ORIGINATOR row + one COUNTERPARTY row per
    # bidirectional edge). control_account_id is the balance-sheet "Due
    # to/from" control account on THIS company's side.
    op.create_table(
        "ic_edges",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
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
        # LOCAL same-tenant partner. NOT NULL for Phase 1 (LOCAL only).
        # Phase 3 (REMOTE) adds a nullable partner_member_id + relaxes this —
        # see services/intercompany.py TODO(remote-relay).
        sa.Column(
            "partner_company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # The BS "Due to/from" control account, on THIS company's CoA.
        # Composite-FK'd below to accounts(id, company_id) so it can never
        # point at a sister company's account.
        sa.Column(
            "control_account_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        # ORIGINATOR / COUNTERPARTY — which end of the reciprocal pair this
        # company plays. Plain String, StrEnum enforced in Python.
        sa.Column(
            "direction",
            sa.String(length=16),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Composite FK: control_account_id must be an account OF company_id.
        # Targets the 0152 uq_accounts_id_company unique constraint.
        sa.ForeignKeyConstraint(
            ["control_account_id", "company_id"],
            ["accounts.id", "accounts.company_id"],
            name="fk_ic_edges_control_account_company",
            ondelete="RESTRICT",
        ),
        # One edge per (company, partner, direction). Leads with company_id so
        # a violation can't enumerate sister-company rows.
        sa.UniqueConstraint(
            "company_id",
            "partner_company_id",
            "direction",
            name="uq_ic_edges_company_partner_direction",
        ),
    )
    op.create_index("ix_ic_edges_tenant_id", "ic_edges", ["tenant_id"])
    op.create_index("ix_ic_edges_company_id", "ic_edges", ["company_id"])
    op.create_index(
        "ix_ic_edges_partner_company_id", "ic_edges", ["partner_company_id"]
    )

    # ------------------------------------------------------------------ ic_legs
    # Links one local journal_entries row to an ic_txn. side = ORIGINATOR /
    # COUNTERPARTY. journal_entry_id is RESTRICT-deleted so a posted leg can
    # never be hard-deleted out from under its pair.
    op.create_table(
        "ic_legs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
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
        sa.Column(
            "ic_txn_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ic_txn.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "journal_entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("journal_entries.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # ORIGINATOR / COUNTERPARTY. Plain String, StrEnum enforced in Python.
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # One leg per (company, ic_txn, side). Leads with company_id.
        sa.UniqueConstraint(
            "company_id",
            "ic_txn_id",
            "side",
            name="uq_ic_legs_company_txn_side",
        ),
        # A journal entry belongs to at most one leg (1:1 JE<->leg).
        sa.UniqueConstraint(
            "company_id",
            "journal_entry_id",
            name="uq_ic_legs_company_journal_entry",
        ),
    )
    op.create_index("ix_ic_legs_tenant_id", "ic_legs", ["tenant_id"])
    op.create_index("ix_ic_legs_company_id", "ic_legs", ["company_id"])
    op.create_index("ix_ic_legs_ic_txn_id", "ic_legs", ["ic_txn_id"])

    # RLS + coherence trigger + GRANT for all three tables.
    for t in _TABLES:
        _apply_rls(t)
        _apply_coherence_trigger(t)
        _grant_app(t)


def downgrade() -> None:
    # Drop in reverse-FK order (children first). DROP TABLE removes the
    # table's triggers + policies with it, but drop policies explicitly first
    # to mirror the 0150 downgrade shape and stay idempotent.
    for t in reversed(_TABLES):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{t}_tenant_coherence ON {t}"))
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {t}"))
        op.execute(sa.text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY"))
    op.drop_table("ic_legs")
    op.drop_table("ic_edges")
    op.drop_table("ic_txn")
    # NOTE: do NOT drop assert_child_tenant_matches_company() — it is owned by
    # 0131 and many other triggers depend on it.
