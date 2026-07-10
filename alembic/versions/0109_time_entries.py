"""time_entries — log billable + non-billable hours per user/project.

Standalone v1, ahead of the full payroll-grade employees table.
A time entry attributes hours to:
  - the user_id who logged it (always set — every entry has a
    logger), AND
  - optionally a contact_id if the actual worker is a contractor
    we already have on file. When payroll-proper lands, this column
    will be supplemented (not replaced) by employee_id.

Billable entries can be marked for conversion to an invoice line; the
conversion writes back invoice_line_id so the same hour isn't
billed twice.

Class-A RLS: time_entries carries tenant_id directly so the
tenant_isolation policy from 0055 applies verbatim, same shape
as expenses.

Note on down_revision: the ci-host WIP had this as 0108_expenses because
the feat/cashbook-persistence branch had intermediate migrations
(0105-0108) that never landed on main/fix-E. Here we bridge directly
from 0104_journal_lines_tax_treatment, which is the actual predecessor
in this branch's chain.

Revision ID: 0109_time_entries
Revises: 0104_journal_lines_tax_treatment
Create Date: 2026-05-21
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0109_time_entries"
down_revision: str | None = "0104_journal_lines_tax_treatment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPROVAL_STATUSES = ("DRAFT", "SUBMITTED", "APPROVED", "REJECTED", "LOCKED")
_DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"


def upgrade() -> None:
    approval_status = postgresql.ENUM(
        *APPROVAL_STATUSES, name="time_entry_approval_status_enum"
    )
    approval_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "time_entries",
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
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
            server_default=sa.text(f"'00000000-0000-0000-0000-000000000001'"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="RESTRICT"),
        ),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("hours", sa.Numeric(5, 2), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=True),
        sa.Column("end_time", sa.Time(), nullable=True),
        sa.Column(
            "break_minutes",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "department_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("departments.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "cost_centre_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cost_centres.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "billable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("rate", sa.Numeric(10, 4)),
        sa.Column(
            "invoice_line_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invoice_lines.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "approval_status",
            postgresql.ENUM(
                *APPROVAL_STATUSES,
                name="time_entry_approval_status_enum",
                create_type=False,
            ),
            nullable=False,
            server_default="DRAFT",
        ),
        sa.Column(
            "submitted_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "approved_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "approved_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("rejection_reason", sa.Text()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
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
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("hours > 0", name="ck_time_entries_hours_positive"),
        sa.CheckConstraint(
            "(start_time IS NULL) = (end_time IS NULL)",
            name="ck_time_entries_clock_pair",
        ),
        sa.CheckConstraint(
            "break_minutes >= 0",
            name="ck_time_entries_break_nonneg",
        ),
    )

    op.create_index(
        "ix_time_entries_user_date",
        "time_entries",
        ["company_id", "user_id", "work_date"],
    )
    op.create_index(
        "ix_time_entries_project_date",
        "time_entries",
        ["company_id", "project_id", "work_date"],
        postgresql_where=sa.text("project_id IS NOT NULL"),
    )
    op.create_index(
        "ix_time_entries_uninvoiced_billable",
        "time_entries",
        ["company_id", "billable", "approval_status"],
        postgresql_where=sa.text("invoice_line_id IS NULL"),
    )
    op.create_index(
        "ix_time_entries_contact_date",
        "time_entries",
        ["company_id", "contact_id", "work_date"],
        postgresql_where=sa.text("contact_id IS NOT NULL"),
    )

    op.execute("ALTER TABLE time_entries ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE time_entries FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON time_entries "
        "FOR ALL USING (tenant_id = current_setting('app.current_tenant', true)::uuid) "
        "WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::uuid)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON time_entries")
    op.execute("ALTER TABLE time_entries NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE time_entries DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_time_entries_contact_date", table_name="time_entries")
    op.drop_index(
        "ix_time_entries_uninvoiced_billable", table_name="time_entries"
    )
    op.drop_index("ix_time_entries_project_date", table_name="time_entries")
    op.drop_index("ix_time_entries_user_date", table_name="time_entries")
    op.drop_table("time_entries")

    postgresql.ENUM(
        *APPROVAL_STATUSES, name="time_entry_approval_status_enum"
    ).drop(op.get_bind(), checkfirst=True)
