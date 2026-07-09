"""0168_company_remittance — remittance / "How to Pay" details on companies.

Why this migration exists
-------------------------
Richard 2026-06-19: the invoice PDF needs a prominent bank-details "How to Pay"
panel plus a standing payment-terms / Terms-of-Trade block so customers can
actually pay and so the late-payment terms (2.5%/month per signed Terms of Trade
#43028) are stated on every Tax Invoice. None of that lived on the company row —
the supplier's own bank account, account name, BSB, bank, the standing terms
text and the Terms-of-Trade URL had nowhere to be stored.

This adds six nullable String columns to ``companies``:

  * bank_name            — e.g. "Westpac"
  * bank_bsb             — e.g. "034-193"
  * bank_account_number  — e.g. "485846"
  * bank_account_name    — e.g. "Example Pty Ltd"
  * payment_terms_text   — standing fine-print terms shown on Tax Invoices
  * terms_url            — link to the full Terms of Trade

All NULL = nothing rendered (the panel is guarded in the template). Columns are
additive + nullable; ``companies`` already enforces FORCE ROW LEVEL SECURITY so
no RLS/policy/CHECK change is needed for new columns. Zero-downtime, no backfill.

Revision ID: 0168_company_remittance
Revises:     0167_email_drafted_status
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0168_company_remittance"
down_revision: str | None = "0167_email_drafted_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("bank_name", sa.String(), nullable=True))
    op.add_column("companies", sa.Column("bank_bsb", sa.String(), nullable=True))
    op.add_column("companies", sa.Column("bank_account_number", sa.String(), nullable=True))
    op.add_column("companies", sa.Column("bank_account_name", sa.String(), nullable=True))
    op.add_column("companies", sa.Column("payment_terms_text", sa.String(), nullable=True))
    op.add_column("companies", sa.Column("terms_url", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("companies", "terms_url")
    op.drop_column("companies", "payment_terms_text")
    op.drop_column("companies", "bank_account_name")
    op.drop_column("companies", "bank_account_number")
    op.drop_column("companies", "bank_bsb")
    op.drop_column("companies", "bank_name")
