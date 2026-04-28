"""Add audit_log table for admin hard-delete forensics.

Gap ADMIN-DELETE-1 (owner-feature-request): admins on SAE Books need to be able
to hard-delete every entity that has a soft-delete route today. The audit_log
table captures a JSONB snapshot of every row at delete time so forensics
survive even when the live row is gone — that satisfies the audit-trail concern
without blocking the workflow.

Schema lifted verbatim from the design doc
(memory/saebooks-hard-delete-policy.md). action='hard_delete' is the first
populated value but the column is intentionally TEXT for future actions
(eg. 'admin_purge', 'gdpr_erasure').

Revision ID: 0072_audit_log
Revises: 0071_trade_in
Create Date: 2026-04-29
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0076_audit_log"
down_revision: str | None = "0075_tracking_vehicle_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("table_name", sa.Text(), nullable=False),
        sa.Column("row_id", sa.Text(), nullable=False),
        sa.Column("row_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_audit_log_tenant_at",
        "audit_log",
        ["tenant_id", sa.text("at DESC")],
    )
    op.create_index(
        "ix_audit_log_table_row",
        "audit_log",
        ["table_name", "row_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_table_row", table_name="audit_log")
    op.drop_index("ix_audit_log_tenant_at", table_name="audit_log")
    op.drop_table("audit_log")
