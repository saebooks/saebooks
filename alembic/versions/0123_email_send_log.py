"""Add email send pipeline scaffolding — audit log + tenant kill-switch.

Builds the gate that prevents accidental customer-facing email during the
pipeline build. Two flags must BOTH be true for a real network send:

  * env  SAEBOOKS_EMAIL_SEND_ENABLED=true   (process-level)
  * row  tenants.outbound_email_enabled=true (per-tenant)

Default for both is false. If either is false, the customer_email service
falls through to outbox mode (write .eml + Resend payload preview to
/opt/data/saebooks-sauer/mail-outbox) and logs the would-be send to
email_send_log with resend_status='blocked'.

email_send_log captures every attempted send (blocked or actual) for audit.
Tenant-scoped with the standard RLS isolation policy.

Revision ID: 0123_email_send_log
Revises: 0122_quote_structured_fields
Create Date: 2026-05-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0123_email_send_log"
down_revision: str | None = "0122_quote_structured_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Two-key gate: per-tenant flag
    op.add_column(
        "tenants",
        sa.Column(
            "outbound_email_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    # Audit log for every email send attempt (blocked or actual)
    op.create_table(
        "email_send_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("doc_type", sa.String(length=32), nullable=False),
        sa.Column("doc_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("doc_version", sa.Integer(), nullable=False),
        sa.Column("sent_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("from_addr", sa.Text(), nullable=False),
        sa.Column("to_addrs", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("cc_addrs", postgresql.ARRAY(sa.Text()),
                  nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("bcc_addrs", postgresql.ARRAY(sa.Text()),
                  nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("attachment_filenames", postgresql.ARRAY(sa.Text()),
                  nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("resend_message_id", sa.Text(), nullable=True),
        sa.Column("resend_status", sa.String(length=16), nullable=False),
        sa.Column("resend_error", sa.Text(), nullable=True),
        sa.Column("kill_switch_reason", sa.Text(), nullable=True,
                  comment="Why the send was blocked, if resend_status='blocked'"),
        sa.Column("sent_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "resend_status IN ('sent','failed','blocked','queued')",
            name="ck_email_send_log_status_valid",
        ),
    )
    op.create_index(
        "ix_email_send_log_tenant_doc",
        "email_send_log",
        ["tenant_id", "doc_type", "doc_id"],
    )
    op.create_index(
        "ix_email_send_log_tenant_sent_at",
        "email_send_log",
        ["tenant_id", "sent_at"],
    )

    # RLS — same pattern as the rest of the tenant-scoped tables
    op.execute("ALTER TABLE email_send_log ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE email_send_log FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON email_send_log
        USING (tenant_id = (current_setting('app.current_tenant', true))::uuid)
        WITH CHECK (tenant_id = (current_setting('app.current_tenant', true))::uuid)
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON email_send_log")
    op.drop_index("ix_email_send_log_tenant_sent_at", table_name="email_send_log")
    op.drop_index("ix_email_send_log_tenant_doc", table_name="email_send_log")
    op.drop_table("email_send_log")
    op.drop_column("tenants", "outbound_email_enabled")
