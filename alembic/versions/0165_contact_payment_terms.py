"""0165_contact_payment_terms — default payment terms on contacts (incl. EOM).

Why this migration exists
-------------------------
Richard 2026-06-07: "we need to be able to set the terms of my suppliers, most
of them are 30 day EOM, not just 30 days." Until now contacts had NO terms field
and nothing in the engine could express end-of-month terms — every bill/invoice
due_date was hand-entered. This adds two nullable columns to ``contacts``:

  * payment_terms_basis  (enum payment_terms_basis_enum: DAYS | EOM)
        DAYS — net N days from the issue date.
        EOM  — N days after the END of the issue month ("30-day EOM").
  * payment_terms_days   (int) — the N.

Both NULL = no default terms (due date stays explicit). The derivation itself
lives in ``services/terms.compute_due_date`` and is applied by bills/invoices
``api_create`` when a due_date is not supplied. Columns are additive + nullable,
so this is a zero-downtime, no-backfill migration; contacts already enforces
FORCE ROW LEVEL SECURITY so no RLS change is needed for new columns.

Revision ID: 0165_contact_payment_terms
Revises:     0164_je_guard_fixes
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0165_contact_payment_terms"
down_revision: str | None = "0164_je_guard_fixes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    basis = sa.Enum("DAYS", "EOM", name="payment_terms_basis_enum")
    basis.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "contacts",
        sa.Column(
            "payment_terms_basis",
            sa.Enum("DAYS", "EOM", name="payment_terms_basis_enum", create_type=False),
            nullable=True,
        ),
    )
    op.add_column(
        "contacts",
        sa.Column(
            "payment_terms_days",
            sa.Integer(),
            nullable=True,
            comment="Days component of the default terms (basis DAYS or EOM)",
        ),
    )


def downgrade() -> None:
    op.drop_column("contacts", "payment_terms_days")
    op.drop_column("contacts", "payment_terms_basis")
    sa.Enum(name="payment_terms_basis_enum").drop(op.get_bind(), checkfirst=True)
