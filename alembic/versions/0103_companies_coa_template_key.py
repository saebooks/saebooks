"""companies.coa_template_key + apply_template dispatch.

Adds a per-company ``coa_template_key`` (default ``au/default``) so the
chart-of-accounts seeder can be dispatched by jurisdiction. Existing
rows backfill to ``au/default`` (the only template wired end-to-end at
v0.1.4). NZ/UK/EE templates land in M1/M2/M3.

Revision ID: 0103_companies_coa_template_key
Revises: 0101_business_identifiers
Create Date: 2026-05-10
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0103_companies_coa_template_key"
down_revision: str | None = "0101_business_identifiers"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column(
            "coa_template_key",
            sa.String(64),
            nullable=False,
            server_default="au/default",
        ),
    )
    # Backfill is implicit via server_default; assert for self-doc.
    op.execute(
        sa.text(
            "UPDATE companies SET coa_template_key = 'au/default' "
            "WHERE coa_template_key IS NULL"
        )
    )


def downgrade() -> None:
    op.drop_column("companies", "coa_template_key")
