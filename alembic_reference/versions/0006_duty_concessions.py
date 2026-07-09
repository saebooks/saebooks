"""duty_concessions reference table (M1.5 · T5).

Per-jurisdiction stamp/transfer/conveyance duty concessions and
exemptions (first-home buyer, off-the-plan, ...), mirroring
``entity_structure_types`` (0003): a per-jurisdiction reference table, NOT
company-scoped. A company-DB ``dutiable_transaction_events`` row
references a concession by id
(``applied_concession_id`` — opaque, no cross-DB FK, validated at the
service layer — no cross-DB FK).

See docs/multi-jurisdiction.md (M1.5) (theme T5).

Revision ID: 0006_duty_concessions
Revises: 0005_ref_tax_code_tax_family
Create Date: 2026-07-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006_duty_concessions"
down_revision: str | None = "0005_ref_tax_code_tax_family"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "duty_concessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "jurisdiction",
            sa.String(3),
            sa.ForeignKey("jurisdictions.code"),
            nullable=False,
        ),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("relief_type", sa.String(32), nullable=False),
        sa.Column("rate_or_amount", sa.Numeric(14, 4), nullable=False),
        sa.Column("description", sa.String(512)),
        sa.UniqueConstraint(
            "jurisdiction", "code", name="uq_duty_concessions_jur_code"
        ),
    )


def downgrade() -> None:
    op.drop_table("duty_concessions")
