"""Phase B + C of the send-log build.

Phase B — immutable attachment snapshot:
  Add bytes + sha256 + content_type arrays to email_send_log so the
  exact PDF that went out is captured with the audit row. Stored
  inline (BYTEA) for now — same transaction as the log insert, no
  cross-service dependency. Can migrate to saebooks-vault later
  without changing the read path.

Phase C — post-send delivery tracking:
  Resend's webhook tells us delivered / bounced / opened / clicked /
  complained. Add columns to record those + a JSONB array of every
  raw webhook event for forensics. Plus webhook signature secret env
  setting (separate migration: env handled at deploy).

Revision ID: 0124_email_log_b_and_c
Revises: 0123_email_send_log
Create Date: 2026-05-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0124_email_log_b_and_c"
down_revision: str | None = "0123_email_send_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ─── Phase B: attachment snapshot bytes ──────────────────────────────
    # Parallel arrays keep the row self-contained — bytes + hash + mime
    # for each attachment, in attachment_filenames order.
    op.add_column(
        "email_send_log",
        sa.Column(
            "attachment_bytes",
            postgresql.ARRAY(postgresql.BYTEA()),
            nullable=False,
            server_default=sa.text("'{}'::bytea[]"),
        ),
    )
    op.add_column(
        "email_send_log",
        sa.Column(
            "attachment_sha256",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )
    op.add_column(
        "email_send_log",
        sa.Column(
            "attachment_content_types",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )

    # ─── Phase C: Resend webhook delivery columns ────────────────────────
    op.add_column("email_send_log", sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("email_send_log", sa.Column("bounced_at",   sa.DateTime(timezone=True), nullable=True))
    op.add_column("email_send_log", sa.Column("bounce_reason", sa.Text(), nullable=True))
    op.add_column("email_send_log", sa.Column("opened_at",    sa.DateTime(timezone=True), nullable=True))
    op.add_column("email_send_log", sa.Column("opened_count", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("email_send_log", sa.Column("clicked_at",   sa.DateTime(timezone=True), nullable=True))
    op.add_column("email_send_log", sa.Column("clicked_count", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("email_send_log", sa.Column("complained_at", sa.DateTime(timezone=True), nullable=True))
    # Raw webhook events for forensics — every event we receive, in order.
    op.add_column(
        "email_send_log",
        sa.Column(
            "webhook_events",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    # Lookup index for the webhook receiver: find a log row by Resend
    # message id. Partial — only rows that actually have a message_id
    # (i.e. ones we attempted to send, not blocked ones).
    op.create_index(
        "ix_email_send_log_resend_message_id",
        "email_send_log",
        ["resend_message_id"],
        postgresql_where=sa.text("resend_message_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_email_send_log_resend_message_id", table_name="email_send_log")
    op.drop_column("email_send_log", "webhook_events")
    op.drop_column("email_send_log", "complained_at")
    op.drop_column("email_send_log", "clicked_count")
    op.drop_column("email_send_log", "clicked_at")
    op.drop_column("email_send_log", "opened_count")
    op.drop_column("email_send_log", "opened_at")
    op.drop_column("email_send_log", "bounce_reason")
    op.drop_column("email_send_log", "bounced_at")
    op.drop_column("email_send_log", "delivered_at")
    op.drop_column("email_send_log", "attachment_content_types")
    op.drop_column("email_send_log", "attachment_sha256")
    op.drop_column("email_send_log", "attachment_bytes")
