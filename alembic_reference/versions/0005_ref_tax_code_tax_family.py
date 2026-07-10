"""ref tax_codes: canonical tax_family + input_credit_recoverable (M1.5 · T1).

Adds the jurisdiction-neutral tax-family discriminator to reference tax
codes so GST / VAT / TVA / IVA resolve to ONE family (vat_gst) while US
sales-&-use tax is a distinct family, and records whether input credits
are recoverable (the property that actually separates the families).

Additive/defaulted: existing seeded codes (all AU GST to date) become
vat_gst with input_credit_recoverable = true.

See ~/records/saebooks/global-reference-audit-2026-07-09.md (theme T1).

Revision ID: 0005_ref_tax_code_tax_family
Revises: 0004_payroll_canonical_tables
Create Date: 2026-07-09
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_ref_tax_code_tax_family"
down_revision: str | None = "0004_payroll_canonical_tables"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tax_codes",
        sa.Column(
            "tax_family",
            sa.String(16),
            nullable=False,
            server_default="vat_gst",
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


def downgrade() -> None:
    op.drop_column("tax_codes", "input_credit_recoverable")
    op.drop_column("tax_codes", "tax_family")
