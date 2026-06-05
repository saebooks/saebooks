"""Transfers — account-to-account money movement record type.

The first-class engine record for moving money between two balance-sheet
accounts of ONE company (bank -> credit-card paydown, bank -> director-loan
repayment, bank/loan transfers). Replaces the spend-money-Expense-to-a-liability
stopgap. Each transfer compiles to ONE balance-sheet JE (Dr to / Cr from, no
GST) via ``services/transfers.py``; ``journal_entry_id`` links the row to that
posted entry. See ``saebooks/models/transfer.py`` and DB-rebuild handover #2.

One tenant-scoped table following the non-negotiable new-table RLS checklist
(same shape as 0150/0154):

  * ``tenant_id`` NOT NULL + FK ``tenants`` (RESTRICT — never let a tenant
    delete out from under its transfer history) and ``company_id`` NOT NULL +
    FK ``companies`` (CASCADE).
  * ``ENABLE`` + ``FORCE`` ROW LEVEL SECURITY + the standard ``tenant_isolation``
    policy (the verbatim 0055/0088/0150/0154 ``app.current_tenant`` predicate,
    with the ``, true`` sentinel and a ``WITH CHECK`` clause).
  * The 0131/0152/0154 ``assert_child_tenant_matches_company`` coherence
    trigger so ``tenant_id`` can never disagree with ``companies.tenant_id`` for
    the row's ``company_id`` (defends the same-tenant wrong-company case).
  * Composite ``(from_account_id, company_id)`` and ``(to_account_id,
    company_id)`` -> ``accounts(id, company_id)`` FKs so the DB itself refuses a
    transfer that points at a sister company's account — the 0152
    ``uq_accounts_id_company`` target already exists.
  * ``journal_entry_id`` FK is ``ondelete=RESTRICT`` so a posted transfer's JE
    can never be hard-deleted out from under it (unwind via reversal).
  * Explicit ``GRANT … TO saebooks_app`` (default privileges silently miss
    tables created under the non-owner migration role — 0138/0152/0154
    precedent).

Reversible: ``downgrade`` drops the table and its trigger/policy.

Revision ID: 0155_transfers
Revises:     0154_intercompany_phase1
Create Date: 2026-06-06
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0155_transfers"
down_revision: str | None = "0154_intercompany_phase1"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Reuse the one-policy-shape-for-the-whole-DB predicate (0055/0088/0150/0154).
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
_WITH_CHECK = _USING

_TABLE = "transfers"


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
    # Reuse the existing 0131 assert_child_tenant_matches_company() function —
    # every row's tenant_id must equal companies.tenant_id for its company_id.
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
    op.create_table(
        "transfers",
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
        # Source (credited on post). Composite-FK'd below to accounts.
        sa.Column(
            "from_account_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        # Destination (debited on post). Composite-FK'd below to accounts.
        sa.Column(
            "to_account_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("transfer_date", sa.Date(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("reference", sa.String(length=64), nullable=True),
        # POSTED / REVERSED. Plain String, StrEnum enforced in Python (mirrors
        # EntryStatus / IcTxnStatus).
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="POSTED",
            nullable=False,
        ),
        # Linkage to the posted balance-sheet JE. RESTRICT so the JE can never
        # be hard-deleted out from under the transfer.
        sa.Column(
            "journal_entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("journal_entries.id", ondelete="RESTRICT"),
            nullable=True,
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
        # Composite FKs: each account must be an account OF company_id. Targets
        # the 0152 uq_accounts_id_company unique constraint.
        sa.ForeignKeyConstraint(
            ["from_account_id", "company_id"],
            ["accounts.id", "accounts.company_id"],
            name="fk_transfers_from_account_company",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["to_account_id", "company_id"],
            ["accounts.id", "accounts.company_id"],
            name="fk_transfers_to_account_company",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_transfers_tenant_id", "transfers", ["tenant_id"])
    op.create_index("ix_transfers_company_id", "transfers", ["company_id"])
    op.create_index(
        "ix_transfers_journal_entry_id", "transfers", ["journal_entry_id"]
    )

    _apply_rls(_TABLE)
    _apply_coherence_trigger(_TABLE)
    _grant_app(_TABLE)


def downgrade() -> None:
    # Drop trigger + policy explicitly first (mirrors the 0150/0154 downgrade
    # shape and stays idempotent), then the table.
    op.execute(
        sa.text(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_tenant_coherence ON {_TABLE}")
    )
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}"))
    op.execute(sa.text(f"ALTER TABLE {_TABLE} NO FORCE ROW LEVEL SECURITY"))
    op.drop_table("transfers")
    # NOTE: do NOT drop assert_child_tenant_matches_company() — it is owned by
    # 0131 and many other triggers depend on it.
