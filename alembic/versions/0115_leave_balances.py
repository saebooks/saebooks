"""Leave balances + accrual events (Phase 4).

Two tables. ``leave_balances`` is the per-employee running balance
per leave type (annual / personal / long_service). ``leave_accruals``
is an append-only audit log of every change to a balance (accrued on
pay-run finalize; debited on paid_leave_lines).

Accrual rule (NES baseline):
- ANNUAL  = 4 weeks/year = 1/13 of ordinary hours worked
- PERSONAL = 10 days/year = 1/26 of ordinary hours worked (approx.)
- LONG_SERVICE = state-by-state; not auto-accrued in v1 (manual)

These rates can be overridden per-employee via ``employees.extra``
JSONB blob — Phase 4.1 will surface that as a UI field.

Revision ID: 0114_leave_balances
Revises: 0113_stp_submissions
Create Date: 2026-05-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0115_leave_balances"
down_revision: str | None = "0114_stp_submissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEAVE_TYPES = ("ANNUAL", "PERSONAL", "LONG_SERVICE", "PARENTAL", "OTHER")
ACCRUAL_KIND = ("ACCRUE", "TAKE", "ADJUST", "PAYOUT")

_DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"


def upgrade() -> None:
    postgresql.ENUM(*LEAVE_TYPES, name="leave_type_enum").create(
        op.get_bind(), checkfirst=True
    )
    postgresql.ENUM(*ACCRUAL_KIND, name="leave_accrual_kind_enum").create(
        op.get_bind(), checkfirst=True
    )

    op.create_table(
        "leave_balances",
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
            "employee_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("employees.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "leave_type",
            postgresql.ENUM(*LEAVE_TYPES, name="leave_type_enum", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "balance_hours", sa.Numeric(10, 2),
            nullable=False, server_default="0",
        ),
        # Opening-balance baseline at the point the employee was added.
        # Used for migrating prior-system leave balances.
        sa.Column(
            "opening_balance_hours", sa.Numeric(10, 2),
            nullable=False, server_default="0",
        ),
        sa.Column(
            "opening_balance_as_at", sa.Date(),
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
        sa.UniqueConstraint(
            "employee_id", "leave_type",
            name="uq_leave_balances_employee_leave_type",
        ),
    )
    op.create_index(
        "ix_leave_balances_company_employee",
        "leave_balances",
        ["company_id", "employee_id"],
    )
    op.create_index(
        "ix_leave_balances_tenant",
        "leave_balances",
        ["tenant_id"],
    )

    op.create_table(
        "leave_accruals",
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
            "balance_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leave_balances.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kind",
            postgresql.ENUM(*ACCRUAL_KIND, name="leave_accrual_kind_enum", create_type=False),
            nullable=False,
        ),
        # Positive for ACCRUE / ADJUST(+), negative for TAKE / PAYOUT / ADJUST(-).
        sa.Column("hours", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "pay_run_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pay_runs.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "pay_run_line_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pay_run_lines.id", ondelete="SET NULL"),
        ),
        sa.Column("reason", sa.Text()),
        sa.Column(
            "balance_after", sa.Numeric(10, 2),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
    )
    op.create_index(
        "ix_leave_accruals_balance",
        "leave_accruals",
        ["balance_id", "created_at"],
    )
    op.create_index(
        "ix_leave_accruals_pay_run",
        "leave_accruals",
        ["pay_run_id"],
        postgresql_where=sa.text("pay_run_id IS NOT NULL"),
    )
    op.create_index(
        "ix_leave_accruals_tenant",
        "leave_accruals",
        ["tenant_id"],
    )

    for tbl in ("leave_balances", "leave_accruals"):
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {tbl} "
            f"FOR ALL USING {_USING} WITH CHECK {_USING}"
        )


def downgrade() -> None:
    for tbl in ("leave_accruals", "leave_balances"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {tbl}")
        op.execute(f"ALTER TABLE {tbl} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_leave_accruals_tenant", table_name="leave_accruals")
    op.drop_index("ix_leave_accruals_pay_run", table_name="leave_accruals")
    op.drop_index("ix_leave_accruals_balance", table_name="leave_accruals")
    op.drop_table("leave_accruals")

    op.drop_index("ix_leave_balances_tenant", table_name="leave_balances")
    op.drop_index(
        "ix_leave_balances_company_employee", table_name="leave_balances"
    )
    op.drop_table("leave_balances")

    postgresql.ENUM(
        *ACCRUAL_KIND, name="leave_accrual_kind_enum"
    ).drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(*LEAVE_TYPES, name="leave_type_enum").drop(
        op.get_bind(), checkfirst=True
    )
