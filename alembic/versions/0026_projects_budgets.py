"""Projects (job costing) + Budgets + project_id FKs on line tables

Revision ID: 0026_projects_budgets
Revises: 0025_user_roles
Create Date: 2026-04-21

Foundation for advanced reporting (Batch FF):

* ``projects`` — a Job/Project against which journal/invoice/bill lines
  can be tagged. Gives a P&L-by-project report and lets us generalise
  to P&L-by-segment later (project, contact, future custom fields).
* ``budgets`` — monthly amount per (company, account, year, month).
  Granularity is monthly because AU BAS is quarterly and ops reporting
  is monthly; annual budgets are just twelve identical rows.
* ``project_id`` nullable FK added to ``journal_lines``,
  ``invoice_lines``, ``bill_lines``. ``ON DELETE SET NULL`` — never
  destroy GL history when an admin archives a project.

One combined migration because the three changes are the same batch
and the rollback story wants them atomic.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0026_projects_budgets"
down_revision: str | None = "0025_user_roles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PROJECT_STATUSES = ("ACTIVE", "COMPLETED", "ARCHIVED")


def upgrade() -> None:
    # --- projects ---------------------------------------------------------
    op.create_table(
        "projects",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "company_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="ACTIVE",
        ),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "extra",
            sa.dialects.postgresql.JSONB(),
            nullable=True,
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
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("company_id", "code", name="uq_projects_company_code"),
        sa.CheckConstraint(
            "status IN ('" + "', '".join(PROJECT_STATUSES) + "')",
            name="ck_projects_status_valid",
        ),
    )
    op.create_index(
        "ix_projects_company_active",
        "projects",
        ["company_id"],
        postgresql_where=sa.text("archived_at IS NULL"),
    )

    # --- budgets ----------------------------------------------------------
    op.create_table(
        "budgets",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "company_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("year", sa.SmallInteger(), nullable=False),
        sa.Column("month", sa.SmallInteger(), nullable=False),
        sa.Column(
            "amount",
            sa.Numeric(18, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
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
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "company_id",
            "account_id",
            "year",
            "month",
            name="uq_budgets_company_account_year_month",
        ),
        sa.CheckConstraint(
            "month BETWEEN 1 AND 12",
            name="ck_budgets_month_valid",
        ),
    )
    op.create_index(
        "ix_budgets_company_period",
        "budgets",
        ["company_id", "year", "month"],
    )

    # --- project_id FKs on line tables ------------------------------------
    # SET NULL on delete — archiving a project never destroys GL history.
    for table in ("journal_lines", "invoice_lines", "bill_lines"):
        op.add_column(
            table,
            sa.Column(
                "project_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                sa.ForeignKey("projects.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.create_index(
            f"ix_{table}_project_id",
            table,
            ["project_id"],
            postgresql_where=sa.text("project_id IS NOT NULL"),
        )


def downgrade() -> None:
    for table in ("journal_lines", "invoice_lines", "bill_lines"):
        op.drop_index(f"ix_{table}_project_id", table_name=table)
        op.drop_column(table, "project_id")
    op.drop_index("ix_budgets_company_period", table_name="budgets")
    op.drop_table("budgets")
    op.drop_index("ix_projects_company_active", table_name="projects")
    op.drop_table("projects")
