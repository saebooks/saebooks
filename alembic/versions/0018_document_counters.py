"""document numbering — per-company sequential counters

Creates ``document_counters`` for gap-free sequential numbering of
AR invoices, AP bills, credit notes and payments. One row per
(company, kind); `next_value` advanced atomically via
``SELECT ... FOR UPDATE`` in ``services/numbering.py``.

Revision ID: 0018_document_counters
Revises: 0017_fixed_asset_register
Create Date: 2026-04-20
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0018_document_counters"
down_revision: str | None = "0017_fixed_asset_register"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_counters",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kind",
            sa.String(32),
            nullable=False,
            comment="'invoice' | 'bill' | 'credit_note' | 'payment' | 'quote'",
        ),
        sa.Column(
            "prefix",
            sa.String(16),
            nullable=False,
            server_default="",
            comment="Literal prefix, e.g. 'INV-' — applied with zero-padded number",
        ),
        sa.Column(
            "next_value",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "pad_width",
            sa.Integer(),
            nullable=False,
            server_default="6",
            comment="Zero-pad width, e.g. 6 -> INV-000042",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("company_id", "kind", name="uq_document_counters_company_kind"),
    )


def downgrade() -> None:
    op.drop_table("document_counters")
