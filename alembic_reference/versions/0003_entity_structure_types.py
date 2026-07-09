"""entity_structure_types reference table (M1.5 · T4).

Per-jurisdiction legal-entity / business-structure types, each mapped to a
jurisdiction-neutral ``canonical_bucket``. Lets the engine know *what kind*
of legal entity a set of books is (Pty Ltd / trust / SMSF / LLC / C-corp /
LLP / pension plan) — the structure that drives accounting and tax
treatment. Companies reference a row by ``companies.entity_structure_code``
(company-DB side, validated at the service layer — no cross-DB FK).

See docs/multi-jurisdiction.md (M1.5) (theme T4).

Revision ID: 0003_entity_structure_types
Revises: 0002_jurisdiction_hierarchy
Create Date: 2026-07-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_entity_structure_types"
down_revision: str | None = "0002_jurisdiction_hierarchy"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entity_structure_types",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "jurisdiction",
            sa.String(3),
            sa.ForeignKey("jurisdictions.code"),
            nullable=False,
        ),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("local_name", sa.String(128), nullable=False),
        sa.Column("canonical_bucket", sa.String(32), nullable=False),
        sa.Column("description", sa.String(512)),
        sa.UniqueConstraint(
            "jurisdiction", "code", name="uq_entity_structure_types_jur_code"
        ),
    )


def downgrade() -> None:
    op.drop_table("entity_structure_types")
