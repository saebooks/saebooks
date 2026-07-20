"""companies.ar_control_account_code / ap_control_account_code — per-company
AR/AP control-account override (Packet 4b).

Why this migration exists
--------------------------
Invoices, bills, payments, credit notes, supplier credit notes, bad-debt
write-offs and FX revaluation all resolve the Trade Debtors / Trade
Creditors control accounts by hardcoding the AU chart-of-accounts
convention codes ``"1-1200"`` / ``"2-1200"`` (seven separate
``_AR_CODE`` / ``_AP_CODE`` module constants across the engine). An EE
(or any non-AU) company whose chart uses a different code for its
receivables/payables control account had no way to tell the engine —
the EE demo build had to re-code its chart to match AU instead.

Two nullable ``String(64)`` columns on ``companies``, following the
same "settings as company columns, NULL = engine resolves the default"
pattern as ``bad_debt_recovery_account`` (0169):

  * ``ar_control_account_code``  NULL = engine resolves ``"1-1200"``
  * ``ap_control_account_code``  NULL = engine resolves ``"2-1200"``

``saebooks.services.control_accounts`` is the single resolver every
posting site now goes through. Every existing row is NULL on upgrade,
so every existing company (all AU today) resolves to the exact same
codes it always did — AU behaviour is byte-identical.

Revision ID: 0198_company_control_accounts
Revises: 0197_ee_fringe_benefit_cols
Create Date: 2026-07-11
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0198_company_control_accounts"
down_revision: str | None = "0197_ee_fringe_benefit_cols"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column("ar_control_account_code", sa.String(64), nullable=True),
    )
    op.add_column(
        "companies",
        sa.Column("ap_control_account_code", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("companies", "ap_control_account_code")
    op.drop_column("companies", "ar_control_account_code")
