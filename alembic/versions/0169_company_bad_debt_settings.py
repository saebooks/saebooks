"""0169_company_bad_debt_settings — per-company bad-debt write-off & recovery policy.

Why this migration exists
-------------------------
Bad Debt Write-off & Recovery (Phase 2 / Task 7). The write-off and recovery
*ledger postings* already shipped in the engine (origins BAD_DEBT_WRITEOFF /
BAD_DEBT_RECOVERY + the bad_debt service). What was missing is where the web
app stores the per-company *policy* that drives those postings:

  * writeoff_mode            review | auto | manual          (default review)
  * writeoff_threshold_days  positive int                    (default 90)
  * recovery_mode            smart_prompt | manual | reopen  (default smart_prompt)
  * bad_debt_recovery_account  optional account code/id      (NULL = engine
                               resolves 4-1290 Bad Debt Recovery on demand)

These follow the same "settings as company columns" pattern as psi_status
(0073) and the remittance fields (0168). Additive + reversible — the three
string/int columns carry server_defaults so existing rows back-fill cleanly,
and bad_debt_recovery_account is nullable.

Revision ID: 0169_company_bad_debt_settings
Revises: 0168_company_remittance
Create Date: 2026-06-23
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0169_company_bad_debt_settings"
down_revision: str | None = "0168_company_remittance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column(
            "writeoff_mode",
            sa.String(16),
            nullable=False,
            server_default="review",
        ),
    )
    op.add_column(
        "companies",
        sa.Column(
            "writeoff_threshold_days",
            sa.Integer(),
            nullable=False,
            server_default="90",
        ),
    )
    op.add_column(
        "companies",
        sa.Column(
            "recovery_mode",
            sa.String(16),
            nullable=False,
            server_default="smart_prompt",
        ),
    )
    op.add_column(
        "companies",
        sa.Column(
            "bad_debt_recovery_account",
            sa.String(64),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("companies", "bad_debt_recovery_account")
    op.drop_column("companies", "recovery_mode")
    op.drop_column("companies", "writeoff_threshold_days")
    op.drop_column("companies", "writeoff_mode")
