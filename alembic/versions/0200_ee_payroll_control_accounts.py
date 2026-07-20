"""companies.ee_payroll_*_account_code — per-company EE payroll GL
control-account overrides (Fixer round 4, F1).

Why this migration exists
--------------------------
``_account_by_setting`` in ``saebooks.services.pay_runs_v2`` resolved
all 13 EE payroll GL control accounts (wages/social-tax/unemployment
expense legs, income-tax/unemployment/pillar-II/social-tax/net-pay
liability legs, plus the 4 fringe-benefit tax legs) from a GLOBAL
``Setting`` row (``settings.key`` is the table's sole primary key — no
``company_id`` column at all, see ``saebooks/models/settings.py``).
Two EE-jurisdiction companies on one instance could not configure
these independently, and if a resolved code happened to already exist
in a second company's own chart for an unrelated purpose, that
company's payroll finalize would silently book to the WRONG account —
no error, journal still balances. This is the exact class of bug 0198
(``ar_control_account_code`` / ``ap_control_account_code``) fixed for
AR/AP; the EE payroll settings added the same week regressed to the
older global-``Setting`` pattern.

13 nullable ``String(64)`` columns on ``companies``, same
"NULL = unresolved, raise loudly" contract ``_account_by_setting``
already had — EE payroll has no default chart-of-accounts seed to
fall back on (unlike AR/AP's AU-code fallback), so there is no sensible
default value to seed here. Every existing row is NULL on upgrade; no
company had these columns before, so no company's resolution behaviour
changes except that a value configured post-migration is now scoped to
the company that sets it instead of leaking instance-wide.

Revision ID: 0200_ee_payroll_control_accounts
Revises: 0199_tax_return_filed_status
Create Date: 2026-07-12
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0200_ee_payroll_control_accounts"
down_revision: str | None = "0199_tax_return_filed_status"
branch_labels = None
depends_on = None

_COLUMNS = [
    "ee_payroll_wages_expense_account_code",
    "ee_payroll_social_tax_expense_account_code",
    "ee_payroll_unemployment_employer_expense_account_code",
    "ee_payroll_income_tax_payable_account_code",
    "ee_payroll_unemployment_employee_payable_account_code",
    "ee_payroll_pillar_ii_payable_account_code",
    "ee_payroll_social_tax_payable_account_code",
    "ee_payroll_unemployment_employer_payable_account_code",
    "ee_payroll_net_pay_clearing_account_code",
    "ee_payroll_fringe_benefit_income_tax_expense_account_code",
    "ee_payroll_fringe_benefit_social_tax_expense_account_code",
    "ee_payroll_fringe_benefit_income_tax_payable_account_code",
    "ee_payroll_fringe_benefit_social_tax_payable_account_code",
]


def upgrade() -> None:
    for col in _COLUMNS:
        op.add_column("companies", sa.Column(col, sa.String(64), nullable=True))


def downgrade() -> None:
    for col in reversed(_COLUMNS):
        op.drop_column("companies", col)
