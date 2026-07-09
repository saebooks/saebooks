"""withholding_tables + social_contribution_schemes canonical tables (M1.5 · T7).

AU payroll reference data lives under AU nouns (``payg_withholding_scales``,
``medicare_levy``) with an AU-shaped column layout. This adds two
jurisdiction-neutral canonical tables alongside them — NOT a rename, NOT a
replacement — so other countries' wage-withholding rules and employee
social-insurance schemes can be stored without forcing them into the ATO's
coefficient/levy shape. See ``withholding_table.py`` and
``social_contribution_scheme.py`` for the full rationale.

Both are reference tables (jurisdiction-keyed, not tenant-scoped) — no RLS.

See docs/multi-jurisdiction.md (M1.5) (theme T7).

Revision ID: 0004_payroll_canonical_tables
Revises: 0003_entity_structure_types
Create Date: 2026-07-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_payroll_canonical_tables"
down_revision: str | None = "0003_entity_structure_types"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "withholding_tables",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "jurisdiction",
            sa.String(3),
            sa.ForeignKey("jurisdictions.code"),
            nullable=False,
        ),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column(
            "withholding_type",
            sa.String(32),
            nullable=False,
            server_default="wage_paye",
        ),
        sa.Column("formula_type", sa.String(32), nullable=False),
        sa.Column("parameters", postgresql.JSONB, nullable=False),
        sa.Column("effective_from", sa.Date, nullable=False),
        sa.Column("effective_to", sa.Date),
        sa.UniqueConstraint(
            "jurisdiction", "code", "effective_from",
            name="uq_withholding_tables_jur_code_eff",
        ),
    )

    op.create_table(
        "social_contribution_schemes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "jurisdiction",
            sa.String(3),
            sa.ForeignKey("jurisdictions.code"),
            nullable=False,
        ),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("payer", sa.String(16), nullable=False),
        sa.Column("rate_percent", sa.Numeric(7, 4), nullable=False),
        sa.Column("wage_base_cap", sa.Numeric(14, 2)),
        sa.Column("collection_mechanism", sa.String(24), nullable=False),
        sa.Column("effective_from", sa.Date, nullable=False),
        sa.Column("effective_to", sa.Date),
        sa.UniqueConstraint(
            "jurisdiction", "code", "effective_from",
            name="uq_social_contribution_schemes_jur_code_eff",
        ),
    )


def downgrade() -> None:
    op.drop_table("social_contribution_schemes")
    op.drop_table("withholding_tables")
