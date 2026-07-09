"""dutiable_transaction_events — postable stamp/transfer/conveyance/
securities/insurance duty (M1.5 · T5).

Before this migration ``stamp_duty_rate`` (reference DB) was a rate-lookup
table only — nothing recorded that a jurisdiction actually assessed duty
on a real transaction, and nothing posted a journal for it. This is a
new, ADDITIVE table: one row per assessed duty, owned by ``company_id``,
posting exactly ONE journal entry (Dr debit_account / Cr credit_account)
via the posting chokepoint ``journal.post_in_txn`` — never a
hand-authored journal entry. Same shape as ``transfers`` (0155).

One tenant-scoped table following the non-negotiable new-table RLS
checklist (same shape as 0155/0178):

  * ``tenant_id`` NOT NULL + FK ``tenants`` (RESTRICT) and ``company_id``
    NOT NULL + FK ``companies`` (CASCADE).
  * ``ENABLE`` + ``FORCE`` ROW LEVEL SECURITY + the standard
    ``tenant_isolation`` policy (the verbatim 0055/0088/0150/0155/0178
    ``app.current_tenant`` predicate, with a ``WITH CHECK`` clause).
  * The 0131 ``assert_child_tenant_matches_company`` coherence trigger so
    ``tenant_id`` can never disagree with ``companies.tenant_id`` for the
    row's ``company_id``.
  * Composite ``(debit_account_id, company_id)`` and
    ``(credit_account_id, company_id)`` -> ``accounts(id, company_id)`` FKs
    so the DB itself refuses an event that points at a sister company's
    account (targets the 0152 ``uq_accounts_id_company`` constraint).
  * ``journal_entry_id`` FK is ``ondelete=RESTRICT`` so a posted event's
    JE can never be hard-deleted out from under it (unwind via reversal).
  * Explicit ``GRANT … TO saebooks_app`` (default privileges silently miss
    tables created under the non-owner migration role).

Reversible: ``downgrade`` drops the table and its trigger/policy.

See docs/multi-jurisdiction.md (M1.5) (theme T5).

Revision ID: 0181_dutiable_transaction_events
Revises:     0180_journal_line_tax_components
Create Date: 2026-07-09
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0182_dutiable_transaction_events"
down_revision: str | None = "0181_business_ident_tax_cols"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Reuse the one-policy-shape-for-the-whole-DB predicate (0055/0088/0150/0155/0178).
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
_WITH_CHECK = _USING

_TABLE = "dutiable_transaction_events"


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
        _TABLE,
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
        sa.Column("event_date", sa.Date(), nullable=False),
        # Free-text — validated at the service layer against DutyType.
        # Kept as String (not a Postgres enum) so a new duty type is a
        # code-only change, mirroring transfers.status.
        sa.Column("duty_type", sa.String(32), nullable=False),
        # Country-level jurisdiction code, e.g. 'AUS'. Free-text, non-FK —
        # the reference DB is a separate database (see model docstring).
        sa.Column("jurisdiction", sa.String(3), nullable=False),
        # Optional state/province-level child jurisdiction code (T3
        # hierarchy), e.g. 'AUQ' for Queensland. Free-text, non-FK.
        sa.Column("sub_jurisdiction", sa.String(3), nullable=True),
        sa.Column("dutiable_value", sa.Numeric(14, 2), nullable=False),
        sa.Column("computed_duty", sa.Numeric(14, 2), nullable=False),
        # Opaque pointer at a RefDutyConcession row (reference DB) —
        # non-FK for the same cross-DB reason as jurisdiction above.
        sa.Column(
            "applied_concession_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("reference", sa.String(length=64), nullable=True),
        # POSTED / REVERSED. Plain String, StrEnum enforced in Python
        # (mirrors transfers.status).
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="POSTED",
            nullable=False,
        ),
        # Debited on post (duty cost or capitalised asset). Composite-FK'd
        # below to accounts.
        sa.Column(
            "debit_account_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        # Credited on post (payable/payment account). Composite-FK'd below
        # to accounts.
        sa.Column(
            "credit_account_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        # Linkage to the posted JE. RESTRICT so the JE can never be
        # hard-deleted out from under the event.
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
        # Composite FKs: each account must be an account OF company_id.
        # Targets the 0152 uq_accounts_id_company constraint.
        sa.ForeignKeyConstraint(
            ["debit_account_id", "company_id"],
            ["accounts.id", "accounts.company_id"],
            name="fk_dutiable_txn_events_debit_account_company",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["credit_account_id", "company_id"],
            ["accounts.id", "accounts.company_id"],
            name="fk_dutiable_txn_events_credit_account_company",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(f"ix_{_TABLE}_tenant_id", _TABLE, ["tenant_id"])
    op.create_index(f"ix_{_TABLE}_company_id", _TABLE, ["company_id"])
    op.create_index(f"ix_{_TABLE}_journal_entry_id", _TABLE, ["journal_entry_id"])

    _apply_rls(_TABLE)
    _apply_coherence_trigger(_TABLE)
    _grant_app(_TABLE)


def downgrade() -> None:
    # Drop trigger + policy explicitly first (mirrors the 0155/0178
    # downgrade shape and stays idempotent), then the table.
    op.execute(
        sa.text(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_tenant_coherence ON {_TABLE}")
    )
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}"))
    op.execute(sa.text(f"ALTER TABLE {_TABLE} NO FORCE ROW LEVEL SECURITY"))
    op.drop_table(_TABLE)
    # NOTE: do NOT drop assert_child_tenant_matches_company() — it is
    # owned by 0131 and many other triggers depend on it.
