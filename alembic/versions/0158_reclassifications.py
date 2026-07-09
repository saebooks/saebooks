"""Reclassifications — account-to-account classification move of an
already-posted amount, WITHOUT mutating the original posted entry.

Gap 2 from the 0156 ledger-cleanup (see ``saebooks-0157-builder-prompt.md``).
The cleanup re-points ~983 posted expenses into new child accounts. Today the
only engine-clean way is void+recreate — heavy, and it leaves void clutter for
a pure classification change. This migration adds a first-class
``reclassifications`` record (approach b): the move compiles to ONE balanced,
engine-generated reclass JE via the posting chokepoint
(``services/reclassifications.py``); ``journal_entry_id`` links the row to that
posted entry. The ORIGINAL posted entry is left untouched (audit-preserved) —
the reclass nets the old account to zero and lands the amount on the new
account. See ``saebooks/models/reclassification.py``.

Sign convention (direction follows the natural balance side of the pair):
  * debit-natured pair (ASSET/EXPENSE/COST_OF_SALES/OTHER_EXPENSE):
    Dr to_account / Cr from_account (the primary ~983-expense case);
  * credit-natured pair (LIABILITY/EQUITY/INCOME/OTHER_INCOME):
    Dr from_account / Cr to_account (mirror — still nets ``from`` to zero).

One tenant-scoped table following the non-negotiable new-table RLS checklist
(same shape as 0150/0154/0155/0157):

  * ``tenant_id`` NOT NULL + FK ``tenants`` (RESTRICT — never let a tenant
    delete out from under its reclassification history) and ``company_id``
    NOT NULL + FK ``companies`` (CASCADE).
  * ``ENABLE`` + ``FORCE`` ROW LEVEL SECURITY + the standard
    ``tenant_isolation`` policy (the verbatim 0055/0088/0150/0155
    ``app.current_tenant`` predicate, with the ``, true`` sentinel and a
    ``WITH CHECK`` clause).
  * The 0131 ``assert_child_tenant_matches_company`` coherence trigger so
    ``tenant_id`` can never disagree with ``companies.tenant_id`` for the
    row's ``company_id``.
  * Composite ``(from_account_id, company_id)`` and ``(to_account_id,
    company_id)`` -> ``accounts(id, company_id)`` FKs so the DB itself refuses
    a reclassification that points at a sister company's account — the 0152
    ``uq_accounts_id_company`` target already exists.
  * ``journal_entry_id`` FK is ``ondelete=RESTRICT`` so the posted reclass JE
    can never be hard-deleted out from under it (unwind via reversal).
  * ``source_entry_id`` FK is ``ondelete=SET NULL`` — it is traceability only
    (the original entry being reclassified, never mutated); archiving the
    source must not be blocked, and the provenance row survives.
  * Explicit ``GRANT … TO saebooks_app`` (default privileges silently miss
    tables created under the non-owner migration role — 0138/0152/0155
    precedent).

Reversible: ``downgrade`` drops the table and its trigger/policy.

Revision ID: 0158_reclassifications
Revises:     0157_moneyin_and_review_flag
Create Date: 2026-06-06
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0158_reclassifications"
down_revision: str | None = "0157_moneyin_and_review_flag"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Reuse the one-policy-shape-for-the-whole-DB predicate (0055/0088/0150/0155).
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
_WITH_CHECK = _USING

_TABLE = "reclassifications"


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
        "reclassifications",
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
        # Account the amount moves OUT of. Composite-FK'd below to accounts.
        sa.Column(
            "from_account_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        # Account the amount moves INTO (typically a child). Composite-FK'd
        # below to accounts.
        sa.Column(
            "to_account_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("reclass_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        # The original JE being reclassified — traceability only, never
        # mutated. SET NULL so archiving the source never destroys the row.
        sa.Column(
            "source_entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("journal_entries.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Linkage to the posted reclass JE. RESTRICT so the JE can never be
        # hard-deleted out from under the reclassification.
        sa.Column(
            "journal_entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("journal_entries.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        # POSTED / REVERSED. Plain String, StrEnum enforced in Python (mirrors
        # EntryStatus / TransferStatus).
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="POSTED",
            nullable=False,
        ),
        sa.Column("created_by", sa.String(), nullable=True),
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
            name="fk_reclassifications_from_account_company",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["to_account_id", "company_id"],
            ["accounts.id", "accounts.company_id"],
            name="fk_reclassifications_to_account_company",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_reclassifications_tenant_id", "reclassifications", ["tenant_id"]
    )
    op.create_index(
        "ix_reclassifications_company_id", "reclassifications", ["company_id"]
    )
    op.create_index(
        "ix_reclassifications_journal_entry_id",
        "reclassifications",
        ["journal_entry_id"],
    )

    _apply_rls(_TABLE)
    _apply_coherence_trigger(_TABLE)
    _grant_app(_TABLE)


def downgrade() -> None:
    # Drop trigger + policy explicitly first (mirrors the 0150/0155 downgrade
    # shape and stays idempotent), then the table.
    op.execute(
        sa.text(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_tenant_coherence ON {_TABLE}")
    )
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}"))
    op.execute(sa.text(f"ALTER TABLE {_TABLE} NO FORCE ROW LEVEL SECURITY"))
    op.drop_table("reclassifications")
    # NOTE: do NOT drop assert_child_tenant_matches_company() — it is owned by
    # 0131 and many other triggers depend on it.
