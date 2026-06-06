"""Money-in record types (supplier credit note + generic receipt) and a
review-flag field on transactions / invoices / expenses.

Two engine gaps from the 0156 ledger-cleanup (see
``saebooks-0157-builder-prompt.md``):

Gap 1 (money-in / negative-expense) — nothing in the engine could post the
``Dr bank/asset, Cr expense / Cr GST-Paid / Cr income`` shape (supplier
refunds, rebates, cashbacks, ATO GST refunds, insurance recoveries). This
migration adds TWO first-class record types, each compiling to its own
balanced JE via the posting chokepoint:

  * ``supplier_credit_notes`` + ``supplier_credit_note_lines`` — the
    purchase-side mirror of the customer ``credit_notes`` table. Reverses a
    purchase: Dr AP control, Cr expense, Cr GST Paid (reverse input credit).
  * ``receipts`` + ``receipt_lines`` — a generic money-in record that credits
    an income OR expense account (with an optional GST line) and debits a
    bank/asset, for refunds/cashbacks/recoveries not tied to a bill.

Both parent tables are tenant-scoped and follow the non-negotiable new-table
RLS checklist (same shape as 0150/0154/0155):

  * ``tenant_id`` NOT NULL + FK ``tenants`` (RESTRICT) and ``company_id``
    NOT NULL + FK ``companies`` (CASCADE).
  * ``ENABLE`` + ``FORCE`` ROW LEVEL SECURITY + the verbatim
    ``tenant_isolation`` policy (``app.current_tenant`` predicate, ``, true``
    sentinel, ``WITH CHECK``).
  * The 0131 ``assert_child_tenant_matches_company`` coherence trigger so
    ``tenant_id`` can never disagree with ``companies.tenant_id``.
  * Composite ``(account_id, company_id) -> accounts(id, company_id)`` FK on
    the bank/asset destination column so a receipt can never debit a sister
    company's account.
  * Explicit ``GRANT ... TO saebooks_app``.

The *line* child tables (``supplier_credit_note_lines`` / ``receipt_lines``)
mirror the existing ``credit_note_lines`` precedent: no own ``tenant_id`` column
(they are reached only through the RLS-guarded parent, exactly like
``credit_note_lines`` / ``invoice_lines``), CASCADE from the parent, and a
composite ``(account_id, company_id)`` FK is NOT added on the line because the
existing ``credit_note_lines`` does not carry ``company_id`` either — the parent's
RLS + the journal-line composite FK at post time are the guards. ``account_id``
keeps a plain RESTRICT FK to ``accounts(id)`` (the customer-CN-line precedent).

Gap 3 (flag for review) — adds ``flagged_for_review`` BOOLEAN NOT NULL DEFAULT
false + ``review_note`` TEXT NULL to the three tables the prompt names:
``journal_entries`` (transactions/JEs), ``invoices`` and ``expenses``. These are
existing tenant-scoped tables already covered by RLS, so this is a plain
``add_column`` on each — no new policy needed.

Reversible: ``downgrade`` drops the flag columns and the four new tables (with
their triggers/policies).

Revision ID: 0157_moneyin_and_review_flag
Revises:     0156_principal_cross_tenant
Create Date: 2026-06-06
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0157_moneyin_and_review_flag"
down_revision: str | None = "0156_principal_cross_tenant"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Reuse the one-policy-shape-for-the-whole-DB predicate (0055/0088/0150/0155).
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
_WITH_CHECK = _USING

_PARENT_TABLES = ("supplier_credit_notes", "receipts")
_FLAG_TABLES = ("journal_entries", "invoices", "expenses")


# --------------------------------------------------------------------------- #
# RLS helpers (verbatim from 0155)
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# upgrade
# --------------------------------------------------------------------------- #
def upgrade() -> None:
    # ---- Gap 1a: supplier (purchase) credit notes --------------------------
    op.create_table(
        "supplier_credit_notes",
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
        # Supplier the credit relates to. RESTRICT (mirrors credit_notes) so a
        # contact with credit-note history can't be deleted out from under it.
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("number", sa.String(length=32), nullable=True),
        sa.Column("issue_date", sa.Date(), nullable=False),
        # DRAFT / POSTED / VOIDED — plain String, StrEnum enforced in Python.
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="DRAFT",
            nullable=False,
        ),
        # The original bill this credit relates to (optional, audit linkage).
        sa.Column(
            "original_bill_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bills.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "supplier_reference", sa.String(length=255), nullable=True
        ),
        sa.Column(
            "subtotal", sa.Numeric(18, 2), server_default="0", nullable=False
        ),
        sa.Column(
            "tax_total", sa.Numeric(18, 2), server_default="0", nullable=False
        ),
        sa.Column(
            "total", sa.Numeric(18, 2), server_default="0", nullable=False
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("posted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("posted_by", sa.String(), nullable=True),
        sa.Column(
            "journal_entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("journal_entries.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "void_journal_entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("journal_entries.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
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
        sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "company_id", "number", name="uq_supplier_credit_notes_company_number"
        ),
    )
    op.create_index(
        "ix_supplier_credit_notes_tenant_id", "supplier_credit_notes", ["tenant_id"]
    )
    op.create_index(
        "ix_supplier_credit_notes_company_id", "supplier_credit_notes", ["company_id"]
    )

    op.create_table(
        "supplier_credit_note_lines",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            primary_key=True,
        ),
        sa.Column(
            "supplier_credit_note_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("supplier_credit_notes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "tax_code_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tax_codes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "quantity", sa.Numeric(18, 4), server_default="1", nullable=False
        ),
        sa.Column(
            "unit_price", sa.Numeric(18, 4), server_default="0", nullable=False
        ),
        sa.Column(
            "discount_pct", sa.Numeric(6, 2), server_default="0", nullable=False
        ),
        sa.Column(
            "line_subtotal", sa.Numeric(18, 2), server_default="0", nullable=False
        ),
        sa.Column(
            "line_tax", sa.Numeric(18, 2), server_default="0", nullable=False
        ),
        sa.Column(
            "line_total", sa.Numeric(18, 2), server_default="0", nullable=False
        ),
    )
    op.create_index(
        "ix_supplier_credit_note_lines_parent",
        "supplier_credit_note_lines",
        ["supplier_credit_note_id"],
    )

    # ---- Gap 1b: generic money-in receipts ---------------------------------
    op.create_table(
        "receipts",
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
        # Destination bank/asset account (debited on post). Composite-FK'd to
        # accounts(id, company_id) below so a receipt can't bank into a sister
        # company's account.
        sa.Column(
            "bank_account_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        # Optional payer contact (refund from a supplier, recovery from an
        # insurer, ATO). RESTRICT so a contact with receipt history survives.
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("number", sa.String(length=32), nullable=True),
        sa.Column("receipt_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="DRAFT",
            nullable=False,
        ),
        sa.Column("reference", sa.String(length=255), nullable=True),
        sa.Column(
            "subtotal", sa.Numeric(18, 2), server_default="0", nullable=False
        ),
        sa.Column(
            "tax_total", sa.Numeric(18, 2), server_default="0", nullable=False
        ),
        sa.Column(
            "total", sa.Numeric(18, 2), server_default="0", nullable=False
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("posted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("posted_by", sa.String(), nullable=True),
        sa.Column(
            "journal_entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("journal_entries.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "void_journal_entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("journal_entries.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
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
        sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "company_id", "number", name="uq_receipts_company_number"
        ),
        # bank_account_id must be an account OF company_id (composite FK to the
        # 0152 uq_accounts_id_company target).
        sa.ForeignKeyConstraint(
            ["bank_account_id", "company_id"],
            ["accounts.id", "accounts.company_id"],
            name="fk_receipts_bank_account_company",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_receipts_tenant_id", "receipts", ["tenant_id"])
    op.create_index("ix_receipts_company_id", "receipts", ["company_id"])

    op.create_table(
        "receipt_lines",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            primary_key=True,
        ),
        sa.Column(
            "receipt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("receipts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        # Income OR expense account credited on post. RESTRICT FK to accounts.
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "tax_code_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tax_codes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "amount", sa.Numeric(18, 2), server_default="0", nullable=False
        ),
        sa.Column(
            "tax_amount", sa.Numeric(18, 2), server_default="0", nullable=False
        ),
        sa.Column(
            "line_total", sa.Numeric(18, 2), server_default="0", nullable=False
        ),
    )
    op.create_index(
        "ix_receipt_lines_parent", "receipt_lines", ["receipt_id"]
    )

    # ---- RLS + coherence + grants on the two parent tables -----------------
    for table in _PARENT_TABLES:
        _apply_rls(table)
        _apply_coherence_trigger(table)
        _grant_app(table)
    # Line tables: no own tenant_id (reached only via the RLS-guarded parent,
    # exactly like credit_note_lines / invoice_lines). Still GRANT to the app
    # role so the non-owner runtime can read/write them through the parent.
    _grant_app("supplier_credit_note_lines")
    _grant_app("receipt_lines")

    # ---- Gap 3: flag-for-review columns on the three named tables -----------
    for table in _FLAG_TABLES:
        op.add_column(
            table,
            sa.Column(
                "flagged_for_review",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            ),
        )
        op.add_column(table, sa.Column("review_note", sa.Text(), nullable=True))


# --------------------------------------------------------------------------- #
# downgrade
# --------------------------------------------------------------------------- #
def downgrade() -> None:
    # Gap 3 columns first.
    for table in _FLAG_TABLES:
        op.drop_column(table, "review_note")
        op.drop_column(table, "flagged_for_review")

    # Line tables (children) before parents.
    op.drop_table("receipt_lines")
    op.drop_table("supplier_credit_note_lines")

    # Parent tables: drop trigger + policy explicitly first (idempotent,
    # mirrors 0155 downgrade), then the table.
    for table in _PARENT_TABLES:
        op.execute(
            sa.text(
                f"DROP TRIGGER IF EXISTS trg_{table}_tenant_coherence ON {table}"
            )
        )
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
        op.execute(sa.text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
    op.drop_table("receipts")
    op.drop_table("supplier_credit_notes")
    # NOTE: do NOT drop assert_child_tenant_matches_company() — owned by 0131.
