"""companies.industry_code + contacts.industry_code (M1.5 P1 tail).

Purely additive, following the 0198 pattern. The ``industry_codes``
reference table (ANZSIC/NZSIC/SIC2007/NACE) already exists but nothing
links a company or contact to a code — these columns are that linkage.
Free text (no FK) because the registry lives in the reference DB;
validated at the service layer, same idiom as ``entity_structure_code``.
NULL for every existing row.

No RLS change — new columns on existing tenant-scoped tables, not new
tables. Reversible: downgrade drops the two columns.

Revision ID: 0213_industry_code_linkage
Revises: 0212_leave_balance_jurisdiction
Create Date: 2026-07-15
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0213_industry_code_linkage"
down_revision: str | None = "0212_leave_balance_jurisdiction"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "companies", sa.Column("industry_code", sa.String(length=16), nullable=True)
    )
    op.add_column(
        "contacts", sa.Column("industry_code", sa.String(length=16), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("contacts", "industry_code")
    op.drop_column("companies", "industry_code")
