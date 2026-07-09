"""0171_invoice_letterhead_parity — letterhead contact fields, default payment
terms, and a per-company Remit-to bank-account designation.

Why this migration exists
-------------------------
Gitea issue #30 sub-items 2/3/4: the document PDFs (invoice / bill / credit
note) render the company name, ABN and address but had nowhere to store the
rest of a normal letterhead, no company-level default for the free-text
payment-terms block, and no way to say "THIS bank account's details go on
the invoice" (0168 only added static ``companies.bank_*`` string columns).

Adds:

``companies`` (all nullable, additive):
  * phone                  — letterhead contact line
  * email                  — letterhead contact line
  * website                — letterhead contact line
  * default_payment_terms  — free text copied onto invoices / credit notes at
                             CREATE when the payload doesn't supply
                             ``payment_terms``. Distinct from
                             ``payment_terms_text`` (0168 standing
                             Terms-of-Trade fine print) and from per-contact
                             DAYS/EOM due-date terms (0165).

``credit_notes``:
  * payment_terms (nullable Text) — per-document free-text terms, mirroring
    ``invoices.payment_terms``; credit notes previously had no such column so
    the PDF hardcoded an empty string.

``accounts``:
  * show_on_invoice (Boolean NOT NULL, server_default false) — marks the ONE
    bank account per company whose BSB / account number / account title feed
    the Remit-to / How-to-Pay panel on invoice and credit-note PDFs. The
    single-flag invariant is enforced at the service layer (setting it true
    clears every sibling). Falls back to the 0168 ``companies.bank_*``
    columns when no account is flagged.

``companies``, ``credit_notes`` and ``accounts`` all enforce FORCE ROW LEVEL
SECURITY (0055), so additive columns need no RLS/policy work. The boolean
carries a server_default so existing rows back-fill cleanly. Zero-downtime,
fully reversible.

Revision ID: 0171_invoice_letterhead_parity
Revises:     0170_ephemeral_demo_tenants
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0171_invoice_letterhead_parity"
down_revision: str | None = "0170_ephemeral_demo_tenants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("phone", sa.String(), nullable=True))
    op.add_column("companies", sa.Column("email", sa.String(), nullable=True))
    op.add_column("companies", sa.Column("website", sa.String(), nullable=True))
    op.add_column("companies", sa.Column("default_payment_terms", sa.Text(), nullable=True))
    op.add_column("credit_notes", sa.Column("payment_terms", sa.Text(), nullable=True))
    op.add_column(
        "accounts",
        sa.Column(
            "show_on_invoice",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("accounts", "show_on_invoice")
    op.drop_column("credit_notes", "payment_terms")
    op.drop_column("companies", "default_payment_terms")
    op.drop_column("companies", "website")
    op.drop_column("companies", "email")
    op.drop_column("companies", "phone")
