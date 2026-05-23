"""cashbook_default_bank_account_id: restrict to cashbook mode only.

Critic finding #29 (2026-05-23 overnight run): two CIVL4 Corp companies
had bookkeeping_mode='full' with a non-null cashbook_default_bank_account_id.
The field is only meaningful in cashbook mode (it names the implicit
counter-account for single-entry UI); setting it on a full-mode company
is a data anomaly and a potential source of confusion when a company is
later flipped back to cashbook.

Fix: add a CHECK constraint that enforces the invariant, and clean up
any existing offenders before the constraint is applied.

Note: the existing migration 0093_cashbook_mode already enforced the
*opposite* direction: bookkeeping_mode='cashbook' REQUIRES a non-null
bank account. This migration adds the *complement*: a non-null bank
account REQUIRES bookkeeping_mode='cashbook'. Together they form a
bi-directional equivalence at the DB layer.

Revision ID: 0126_cashbook_default_bank_check
Revises: 0125_email_log_tamper
Create Date: 2026-05-24
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0126_cashbook_default_bank_check"
down_revision: str | None = "0125_email_log_tamper"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_cashbook_default_bank_requires_cashbook_mode"


def upgrade() -> None:
    # Clean up existing offenders before applying the constraint.
    # Any full-mode (or other non-cashbook) company that has
    # cashbook_default_bank_account_id set is invalid; null it out.
    op.execute(
        "UPDATE companies "
        "SET cashbook_default_bank_account_id = NULL "
        "WHERE bookkeeping_mode != 'cashbook' "
        "AND cashbook_default_bank_account_id IS NOT NULL"
    )

    # Enforce the complement of the existing ck_cashbook_requires_bank
    # constraint: the bank id field may only be set when the company is
    # in cashbook mode.
    op.create_check_constraint(
        _CONSTRAINT,
        "companies",
        "cashbook_default_bank_account_id IS NULL OR bookkeeping_mode = 'cashbook'",
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "companies", type_="check")
