"""0141_account_credit_limit — add credit_limit + credit_limit_kind to accounts.

Rationale (Richard, 2026-05-31):
    Bank accounts of kind CREDIT_CARD (and optionally loans) can carry a
    credit limit. The app should surface "available credit" and warn when
    the balance owed exceeds the limit. This is a *bookkeeping* app, so the
    default limit kind is SOFT — a warning only; data entry is never blocked.
    The soft/hard distinction mirrors ``SeatCapKind`` in
    ``saebooks/services/licence/caps.py`` (Literal["hard", "soft"]).

Schema change:
    - accounts.credit_limit       Numeric(18,2)  NULL  (null = no limit set)
    - accounts.credit_limit_kind  VARCHAR        NULL  server_default 'soft'
        CHECK (credit_limit_kind IN ('soft','hard'))

    A checked VARCHAR is used rather than a native PG enum: it is the
    lower-risk choice for a 2-value flag (no CREATE TYPE, trivial to extend
    or relax) and the spec explicitly endorses it. The CHECK guarantees the
    same integrity a native enum would.

Tenant-scoping: no new table, no new policy. Existing accounts RLS covers it.

Revision ID: 0141_account_credit_limit
Revises: 0140_payments_one_off_customer
Create Date: 2026-05-31
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0143_account_credit_limit"
down_revision: str | None = "0142_invoice_written_off"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("credit_limit", sa.Numeric(18, 2), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column(
            "credit_limit_kind",
            sa.String(length=4),
            server_default="soft",
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_accounts_credit_limit_kind",
        "accounts",
        "credit_limit_kind IN ('soft', 'hard')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_accounts_credit_limit_kind", "accounts", type_="check"
    )
    op.drop_column("accounts", "credit_limit_kind")
    op.drop_column("accounts", "credit_limit")
