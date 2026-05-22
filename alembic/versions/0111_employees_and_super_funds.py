"""Employees + SuperFunds + payroll permission codes (Phase 1A).

Foundation for AU payroll + STP Phase 2. Adds two new tables
(``employees``, ``super_funds``), seven STP2 enums, payroll
permission codes, and grants for all 5 user roles.

The ``time_entries`` table shipped earlier in 0109. Phase 1B will
extend ``pay_run_lines`` (hours / allowances / deductions / leave /
YTD) and retarget its ``employee_id`` FK from ``contacts.id`` to
``employees.id``.

Class-A RLS on both new tables: ENABLE + FORCE + tenant_isolation
policy matching 0055's shape.

Sensitive columns (TFN, employee bank BSB+acct+name, SMSF bank
fields) are opaque ``TEXT`` at the schema layer; the service layer
calls ``saebooks.services.crypto.encrypt_field`` (Fernet keyed off
``SAEBOOKS_FIELD_ENCRYPTION_KEY``) to populate them. Schema cares
only that they are TEXT.

Permission grants are inserted for all 5 roles (owner / admin /
accountant / bookkeeper / viewer). The legacy check constraint
``ck_role_permissions_role`` was already updated by 0058 to accept
this set (verified live on all 5 stacks 2026-05-22).

Revision ID: 0110_employees_and_super_funds
Revises: 0109_time_entries
Create Date: 2026-05-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0111_employees_and_super_funds"
down_revision: str | None = "0110_api_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --- Enum values (kept in sync with saebooks/models/employee.py) ---------- #

TFN_STATUSES = (
    "PROVIDED", "NOT_PROVIDED", "NEW_PAYEE_30D",
    "EXEMPT_PENSIONER", "EXEMPT_UNDER_18", "APPLIED_FOR",
)
EMPLOYMENT_BASES = ("F", "P", "C", "L", "V", "N")
TERMINATION_REASONS = ("V", "I", "D", "R", "F", "C", "T")
INCOME_STREAM_TYPES = (
    "SAW", "CHP", "IAA", "WHM", "SWP", "JPD", "VOL", "LAB", "OSP",
)
PAY_FREQUENCIES = ("WEEKLY", "FORTNIGHTLY", "MONTHLY", "QUARTERLY", "ANNUAL")
PAY_BASES = ("HOURLY", "SALARY")
PAYSLIP_DELIVERIES = ("EMAIL", "PRINT", "PORTAL")

# --- RLS predicates ------------------------------------------------------- #

_DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
_APP_ROLE = "saebooks_app"

# --- New permission codes ------------------------------------------------- #

_NEW_PERMS: list[tuple[str, str]] = [
    ("employee.view",       "View employees (masked TFN)"),
    ("employee.edit",       "Create and edit employees"),
    ("employee.tfn_view",   "Decrypt and view employee TFN (audit-logged)"),
    ("super_fund.view",     "View super funds"),
    ("super_fund.edit",     "Create and edit super funds; set company default"),
]

# Per-role grants. Owner gets everything (super-user). Viewer gets read-only.
_ROLE_GRANTS: dict[str, list[str]] = {
    "owner": [code for code, _ in _NEW_PERMS],
    "admin": [code for code, _ in _NEW_PERMS],
    "accountant": [
        "employee.view", "employee.edit",
        "super_fund.view", "super_fund.edit",
    ],
    "bookkeeper": [
        "employee.view", "employee.edit",
        "super_fund.view",
    ],
    "viewer": [
        "employee.view",
        "super_fund.view",
    ],
}


# ------------------------------------------------------------------------- #
# Helpers                                                                   #
# ------------------------------------------------------------------------- #


def _create_enum(name: str, values: tuple[str, ...]) -> None:
    postgresql.ENUM(*values, name=name).create(op.get_bind(), checkfirst=True)


def _drop_enum(name: str, values: tuple[str, ...]) -> None:
    postgresql.ENUM(*values, name=name).drop(op.get_bind(), checkfirst=True)


def _install_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"FOR ALL USING {_USING} WITH CHECK {_USING}"
    )


# ------------------------------------------------------------------------- #
# upgrade                                                                   #
# ------------------------------------------------------------------------- #


def upgrade() -> None:
    # === 1. Enums ======================================================== #
    _create_enum("tfn_status_enum",              TFN_STATUSES)
    _create_enum("employment_basis_enum",        EMPLOYMENT_BASES)
    _create_enum("termination_reason_enum",      TERMINATION_REASONS)
    _create_enum("income_stream_type_enum",      INCOME_STREAM_TYPES)
    _create_enum("pay_frequency_enum",           PAY_FREQUENCIES)
    _create_enum("pay_basis_enum",               PAY_BASES)
    _create_enum("payslip_delivery_enum",        PAYSLIP_DELIVERIES)

    # === 2. super_funds (created first — employees FK it) ================ #
    op.create_table(
        "super_funds",
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
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("usi", sa.String(11)),
        sa.Column("spin", sa.String(20)),
        sa.Column(
            "is_smsf", sa.Boolean(), nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("employer_abn", sa.String(14)),
        sa.Column("esa", sa.String(16)),
        sa.Column("smsf_bsb_encrypted", sa.Text()),
        sa.Column("smsf_account_number_encrypted", sa.Text()),
        sa.Column("smsf_account_name_encrypted", sa.Text()),
        sa.Column(
            "is_default", sa.Boolean(), nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "version", sa.Integer(), nullable=False,
            server_default="1",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "is_smsf = false OR (employer_abn IS NOT NULL AND esa IS NOT NULL)",
            name="ck_super_funds_smsf_required_fields",
        ),
        sa.CheckConstraint(
            "is_smsf = true OR usi IS NOT NULL",
            name="ck_super_funds_apra_requires_usi",
        ),
        sa.CheckConstraint(
            "usi IS NULL OR length(usi) = 11",
            name="ck_super_funds_usi_length",
        ),
    )
    op.create_index(
        "ix_super_funds_company_active",
        "super_funds",
        ["company_id", "archived_at"],
    )
    op.create_index(
        "ix_super_funds_tenant",
        "super_funds",
        ["tenant_id"],
    )
    op.create_index(
        "uq_super_funds_company_usi",
        "super_funds",
        ["company_id", "usi"],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL AND usi IS NOT NULL"),
    )
    op.create_index(
        "uq_super_funds_default_per_company",
        "super_funds",
        ["company_id"],
        unique=True,
        postgresql_where=sa.text("is_default = true AND archived_at IS NULL"),
    )
    _install_rls("super_funds")

    # === 3. employees ==================================================== #
    op.create_table(
        "employees",
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
            "contact_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("employee_number", sa.String(32), nullable=False),
        sa.Column(
            "payee_id_bms", postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("previous_payee_id", sa.String(50)),
        sa.Column("tfn_encrypted", sa.Text()),
        sa.Column(
            "tfn_status",
            postgresql.ENUM(*TFN_STATUSES, name="tfn_status_enum", create_type=False),
            nullable=False,
            server_default="NOT_PROVIDED",
        ),
        sa.Column("dob", sa.Date()),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date()),
        sa.Column(
            "termination_reason",
            postgresql.ENUM(*TERMINATION_REASONS, name="termination_reason_enum", create_type=False),
        ),
        sa.Column("address_line1", sa.String()),
        sa.Column("address_line2", sa.String()),
        sa.Column("suburb", sa.String(64)),
        sa.Column("state", sa.String(8)),
        sa.Column("postcode", sa.String(8)),
        sa.Column(
            "country_code", sa.String(2),
            nullable=False, server_default="AU",
        ),
        sa.Column(
            "employment_basis",
            postgresql.ENUM(*EMPLOYMENT_BASES, name="employment_basis_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("tax_treatment_code", sa.String(6)),
        sa.Column(
            "claims_tax_free_threshold", sa.Boolean(),
            nullable=False, server_default=sa.text("false"),
        ),
        sa.Column(
            "is_australian_resident", sa.Boolean(),
            nullable=False, server_default=sa.text("true"),
        ),
        sa.Column(
            "study_training_support_loan", sa.Boolean(),
            nullable=False, server_default=sa.text("false"),
        ),
        sa.Column(
            "working_holiday_maker", sa.Boolean(),
            nullable=False, server_default=sa.text("false"),
        ),
        sa.Column("whm_country_code", sa.String(2)),
        sa.Column(
            "income_stream_type",
            postgresql.ENUM(*INCOME_STREAM_TYPES, name="income_stream_type_enum", create_type=False),
            nullable=False,
            server_default="SAW",
        ),
        sa.Column("payg_branch_code", sa.String(3)),
        sa.Column("bsb_encrypted", sa.Text()),
        sa.Column("account_number_encrypted", sa.Text()),
        sa.Column("account_name_encrypted", sa.Text()),
        sa.Column(
            "super_fund_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("super_funds.id", ondelete="RESTRICT"),
        ),
        sa.Column("super_member_number", sa.String(64)),
        sa.Column("payslip_email", sa.String()),
        sa.Column(
            "payslip_delivery",
            postgresql.ENUM(*PAYSLIP_DELIVERIES, name="payslip_delivery_enum", create_type=False),
            nullable=False,
            server_default="EMAIL",
        ),
        sa.Column(
            "pay_frequency",
            postgresql.ENUM(*PAY_FREQUENCIES, name="pay_frequency_enum", create_type=False),
            nullable=False,
            server_default="WEEKLY",
        ),
        sa.Column(
            "pay_basis",
            postgresql.ENUM(*PAY_BASES, name="pay_basis_enum", create_type=False),
            nullable=False,
            server_default="HOURLY",
        ),
        sa.Column("base_rate", sa.Numeric(10, 4), nullable=False),
        sa.Column(
            "weekly_hours", sa.Numeric(5, 2),
            nullable=False, server_default="38.00",
        ),
        sa.Column("notes", sa.Text()),
        sa.Column("extra", postgresql.JSONB()),
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
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "company_id", "employee_number",
            name="uq_employees_company_number",
        ),
        sa.UniqueConstraint(
            "contact_id", name="uq_employees_contact_id",
        ),
        sa.CheckConstraint(
            "end_date IS NULL OR end_date >= start_date",
            name="ck_employees_end_after_start",
        ),
        sa.CheckConstraint(
            "termination_reason IS NULL OR end_date IS NOT NULL",
            name="ck_employees_termination_needs_end_date",
        ),
        sa.CheckConstraint(
            "working_holiday_maker = false OR whm_country_code IS NOT NULL",
            name="ck_employees_whm_country_required",
        ),
        sa.CheckConstraint(
            "pay_basis = 'HOURLY' OR weekly_hours > 0",
            name="ck_employees_salary_needs_weekly_hours",
        ),
        sa.CheckConstraint(
            "base_rate >= 0",
            name="ck_employees_base_rate_nonneg",
        ),
    )
    op.create_index(
        "ix_employees_company_active",
        "employees",
        ["company_id", "archived_at"],
    )
    op.create_index(
        "ix_employees_company_super_fund",
        "employees",
        ["company_id", "super_fund_id"],
    )
    op.create_index(
        "ix_employees_tenant",
        "employees",
        ["tenant_id"],
    )
    _install_rls("employees")

    # === 4. Permission seeds ============================================= #
    conn = op.get_bind()
    for code, description in _NEW_PERMS:
        conn.execute(
            sa.text(
                "INSERT INTO permissions (code, description) "
                "VALUES (:code, :description) "
                "ON CONFLICT (code) DO NOTHING"
            ),
            {"code": code, "description": description},
        )
    for role, grants in _ROLE_GRANTS.items():
        for code in grants:
            conn.execute(
                sa.text(
                    "INSERT INTO role_permissions (role, permission_code) "
                    "VALUES (:role, :code) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"role": role, "code": code},
            )


# ------------------------------------------------------------------------- #
# downgrade                                                                 #
# ------------------------------------------------------------------------- #


def downgrade() -> None:
    conn = op.get_bind()

    # Permission grants + catalogue rows (reverse insertion order)
    for role, grants in _ROLE_GRANTS.items():
        for code in grants:
            conn.execute(
                sa.text(
                    "DELETE FROM role_permissions "
                    "WHERE role = :role AND permission_code = :code"
                ),
                {"role": role, "code": code},
            )
    for code, _ in _NEW_PERMS:
        conn.execute(
            sa.text("DELETE FROM permissions WHERE code = :code"),
            {"code": code},
        )

    # Drop policies + indexes + table (employees first — FKs super_funds)
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON employees")
    op.execute("ALTER TABLE employees NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE employees DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_employees_tenant", table_name="employees")
    op.drop_index("ix_employees_company_super_fund", table_name="employees")
    op.drop_index("ix_employees_company_active", table_name="employees")
    op.drop_table("employees")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON super_funds")
    op.execute("ALTER TABLE super_funds NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE super_funds DISABLE ROW LEVEL SECURITY")
    op.drop_index("uq_super_funds_default_per_company", table_name="super_funds")
    op.drop_index("uq_super_funds_company_usi", table_name="super_funds")
    op.drop_index("ix_super_funds_tenant", table_name="super_funds")
    op.drop_index("ix_super_funds_company_active", table_name="super_funds")
    op.drop_table("super_funds")

    # Enums (drop after table — Postgres won't drop a type still in use)
    _drop_enum("payslip_delivery_enum",     PAYSLIP_DELIVERIES)
    _drop_enum("pay_basis_enum",            PAY_BASES)
    _drop_enum("pay_frequency_enum",        PAY_FREQUENCIES)
    _drop_enum("income_stream_type_enum",   INCOME_STREAM_TYPES)
    _drop_enum("termination_reason_enum",   TERMINATION_REASONS)
    _drop_enum("employment_basis_enum",     EMPLOYMENT_BASES)
    _drop_enum("tfn_status_enum",           TFN_STATUSES)
