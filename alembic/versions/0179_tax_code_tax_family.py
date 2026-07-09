"""company tax_codes: tax_family + input_credit_recoverable (M1.5 · T1).

Company-side counterpart to 0005 on the reference DB. Adds the canonical
tax-family discriminator alongside the legacy free-text ``tax_system``
(kept for back-compat). Additive/defaulted: existing rows -> vat_gst with
input_credit_recoverable = true (backfilled from tax_system so any legacy
'GST'/'VAT' explicitly lands on vat_gst).

See docs/multi-jurisdiction.md (M1.5) (theme T1).

Revision ID: 0179_tax_code_tax_family
Revises: 0178_bank_routing_identifiers
Create Date: 2026-07-09
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0179_tax_code_tax_family"
down_revision: str | None = "0178_bank_routing_identifiers"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tax_codes",
        sa.Column(
            "tax_family", sa.String(16), nullable=False, server_default="vat_gst"
        ),
    )
    op.add_column(
        "tax_codes",
        sa.Column(
            "input_credit_recoverable",
            sa.Boolean,
            nullable=False,
            server_default=sa.true(),
        ),
    )
    # Backfill explicitly (server_default already covers existing rows, but
    # make the GST/VAT -> vat_gst mapping intentional and visible).
    op.execute(
        sa.text(
            "UPDATE tax_codes SET tax_family = 'vat_gst' "
            "WHERE upper(tax_system) IN ('GST', 'VAT')"
        )
    )


def downgrade() -> None:
    op.drop_column("tax_codes", "input_credit_recoverable")
    op.drop_column("tax_codes", "tax_family")
