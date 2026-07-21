"""Not-for-profit charitable-registration attributes on companies (M1.5 P1 tail).

Purely additive, following the 0198 pattern. ``EntityStructureBucket.NONPROFIT``
(reference DB, already live) classifies the entity *structure*; these columns
hold the registration status a nonprofit entity separately obtains from a
regulator — AU: ACNC charity registration + ATO DGR endorsement + tax
concession type. Booleans default false (server_default), the two free-text
columns default NULL — every existing company is unaffected.

No RLS change — new columns on an existing tenant-scoped table, not a new
table. Reversible: downgrade drops the four columns.

Revision ID: 0210_company_nfp_attributes
Revises: 0209_account_equity_subtype
Create Date: 2026-07-15
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0210_company_nfp_attributes"
down_revision: str | None = "0209_account_equity_subtype"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column(
            "acnc_registered", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "companies",
        sa.Column(
            "dgr_endorsed", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "companies", sa.Column("dgr_category", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "companies", sa.Column("tax_concession_type", sa.String(length=32), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("companies", "tax_concession_type")
    op.drop_column("companies", "dgr_category")
    op.drop_column("companies", "dgr_endorsed")
    op.drop_column("companies", "acnc_registered")
