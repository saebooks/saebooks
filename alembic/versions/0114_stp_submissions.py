"""STP Phase 2 submission tracking.

Records every STP payload assembled for a finalized pay run. The
actual ATO submission (SBR3 SOAP via the existing RAM Machine
Credential keystore) lands in Phase 3.1 — this migration just
captures the payload + its lifecycle state so we can re-submit
on demand and have an audit trail.

State machine:
    READY        -- payload built; not yet submitted
    SUBMITTED    -- handed off to ATO; awaiting response
    ACCEPTED     -- ATO returned a Receipt Number
    REJECTED     -- ATO returned validation errors (see errors JSONB)
    SUPERSEDED   -- a later submission replaces this one (e.g. correction)

Class-A RLS as per the rest of the new tables.

Revision ID: 0113_stp_submissions
Revises: 0112_payg_tables
Create Date: 2026-05-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0114_stp_submissions"
down_revision: str | None = "0113_payg_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STP_EVENT_TYPES = ("PAY", "UPDATE", "FINALISATION")
STP_STATUSES = ("READY", "SUBMITTED", "ACCEPTED", "REJECTED", "SUPERSEDED")

_DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"


def upgrade() -> None:
    postgresql.ENUM(*STP_EVENT_TYPES, name="stp_event_type_enum").create(
        op.get_bind(), checkfirst=True
    )
    postgresql.ENUM(*STP_STATUSES, name="stp_status_enum").create(
        op.get_bind(), checkfirst=True
    )

    op.create_table(
        "stp_submissions",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "company_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
            server_default=sa.text(f"'{_DEFAULT_TENANT}'"),
        ),
        sa.Column(
            "pay_run_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pay_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "event_type",
            postgresql.ENUM(*STP_EVENT_TYPES, name="stp_event_type_enum", create_type=False),
            nullable=False,
            server_default="PAY",
        ),
        sa.Column(
            "status",
            postgresql.ENUM(*STP_STATUSES, name="stp_status_enum", create_type=False),
            nullable=False,
            server_default="READY",
        ),
        # The payload is JSON-shaped per STP2 (we'll wrap it in SBR3 XML
        # at submission time). Stored as JSONB for query + diffing.
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
        ),
        # Set once we hand off to ATO.
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("submitted_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("ato_receipt_number", sa.String(64)),
        sa.Column("ato_response_payload", postgresql.JSONB()),
        # Validation errors from ATO (or local pre-flight).
        sa.Column(
            "errors",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        # Supersession: when this submission was replaced by another.
        sa.Column(
            "superseded_by_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("stp_submissions.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "version", sa.Integer(),
            nullable=False, server_default="1",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_index(
        "ix_stp_submissions_pay_run",
        "stp_submissions",
        ["pay_run_id"],
    )
    op.create_index(
        "ix_stp_submissions_company_status",
        "stp_submissions",
        ["company_id", "status"],
    )
    op.create_index(
        "ix_stp_submissions_tenant",
        "stp_submissions",
        ["tenant_id"],
    )

    op.execute("ALTER TABLE stp_submissions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE stp_submissions FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON stp_submissions "
        f"FOR ALL USING {_USING} WITH CHECK {_USING}"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON stp_submissions")
    op.execute("ALTER TABLE stp_submissions NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE stp_submissions DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_stp_submissions_tenant", table_name="stp_submissions")
    op.drop_index(
        "ix_stp_submissions_company_status", table_name="stp_submissions"
    )
    op.drop_index("ix_stp_submissions_pay_run", table_name="stp_submissions")
    op.drop_table("stp_submissions")
    postgresql.ENUM(*STP_STATUSES, name="stp_status_enum").drop(
        op.get_bind(), checkfirst=True
    )
    postgresql.ENUM(
        *STP_EVENT_TYPES, name="stp_event_type_enum"
    ).drop(op.get_bind(), checkfirst=True)
