"""0177_company_entity_structure — companies.entity_structure_code (M1.5 · T4).

Adds a single nullable column to ``companies`` recording the company's
legal-entity / business-structure type (Pty Ltd / trust / SMSF / LLC /
C-corp / LLP / pension plan). The value is a
``RefEntityStructureType.code`` resolved within the company's own
jurisdiction; validation happens at the service layer against the
reference DB, so there is deliberately NO foreign key here (the reference
registry lives in a separate database — same pattern as
``companies.jurisdiction``).

Purely additive and non-breaking: nullable, no default, existing rows
stay NULL ("not yet classified"). No RLS change — this is a new column on
an existing tenant-scoped table, not a new table.

See ~/records/saebooks/global-reference-audit-2026-07-09.md (theme T4).

Revision ID: 0177_company_entity_structure
Revises: 0176_inbox_email
Create Date: 2026-07-09
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0177_company_entity_structure"
down_revision: str | None = "0176_inbox_email"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column("entity_structure_code", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("companies", "entity_structure_code")
