"""Audit snapshots table for risky-edit recovery

Revision ID: 0011_audit_snapshots
Revises: 0010_hyphenated_codes
Create Date: 2026-04-16
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "0011_audit_snapshots"
down_revision: str | None = "0010_hyphenated_codes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_snapshots",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("table_name", sa.String(64), nullable=False),
        sa.Column("row_id", sa.String(64), nullable=False, comment="PK of the snapshotted row"),
        sa.Column("action", sa.String(16), nullable=False, comment="update, delete, migrate"),
        sa.Column("before_data", JSONB, nullable=False, comment="Row state before change"),
        sa.Column("after_data", JSONB, comment="Row state after change (null for deletes)"),
        sa.Column("reason", sa.Text),
        sa.Column("performed_by", sa.String),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_snapshots_table_row", "audit_snapshots", ["table_name", "row_id"])
    op.create_index("ix_audit_snapshots_created", "audit_snapshots", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_snapshots_created")
    op.drop_index("ix_audit_snapshots_table_row")
    op.drop_table("audit_snapshots")
