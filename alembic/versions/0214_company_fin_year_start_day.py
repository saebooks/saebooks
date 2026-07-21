"""companies.fin_year_start_day (M1.5 P1 tail).

Purely additive, following the 0198 pattern. Companion to
``fin_year_start_month`` — day-level fiscal-year anchor precision (the
reference ``FiscalYearDefinition`` already carries ``fy_start_day``).
Defaults to 1 (server_default) — every existing company's month-only
anchor is unchanged (1st of the month, the implicit assumption every
current consumer already makes).

No RLS change — new column on an existing tenant-scoped table, not a new
table. Reversible: downgrade drops the column.

Revision ID: 0214_company_fin_year_start_day
Revises: 0213_industry_code_linkage
Create Date: 2026-07-15
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0214_company_fin_year_start_day"
down_revision: str | None = "0213_industry_code_linkage"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column(
            "fin_year_start_day", sa.Integer(), nullable=False, server_default="1"
        ),
    )


def downgrade() -> None:
    op.drop_column("companies", "fin_year_start_day")
