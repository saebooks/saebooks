"""accounts.account_kind — sub-classification for bank/card/loan/cash.

Adds an ``account_kind`` ENUM column on the ``accounts`` table so that
the Bank Accounts page can list checking accounts, credit cards, bank
loans, and cash under one roof, and SISS-style bank-feed adapters can
pick a feed type per account.

NULLABLE — existing accounts get NULL; the Bank Accounts page filters
on ``account_kind IS NOT NULL``. Migration also opportunistically
auto-classifies existing rows where the kind is obvious:

  - ASSET with bsb IS NOT NULL → BANK_CHECKING
  - ASSET named ``%savings%``  → BANK_SAVINGS  (overrides above)
  - ASSET named ``%cash%`` or ``%petty%`` → CASH
  - LIABILITY named ``%credit card%`` / ``%card%`` → CREDIT_CARD
  - LIABILITY named ``%loan%`` / ``%mortgage%`` → BANK_LOAN

Everything else stays NULL. Operators can backfill manually via the
new Bank Accounts UI.

Revision ID: 0106_account_kind
Revises: zzzz_local_merge_m0_branches
Create Date: 2026-05-20
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0106_account_kind"
down_revision: str | None = "zzzz_local_merge_m0_branches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACCOUNT_KINDS = (
    "BANK_CHECKING",
    "BANK_SAVINGS",
    "CREDIT_CARD",
    "BANK_LOAN",
    "CASH",
    "OTHER",
)


def upgrade() -> None:
    kind_enum = postgresql.ENUM(*ACCOUNT_KINDS, name="account_kind_enum")
    kind_enum.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "accounts",
        sa.Column(
            "account_kind",
            postgresql.ENUM(*ACCOUNT_KINDS, name="account_kind_enum", create_type=False),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_accounts_account_kind",
        "accounts",
        ["company_id", "account_kind"],
        postgresql_where=sa.text("account_kind IS NOT NULL"),
    )

    # Auto-classification pass. Order matters: more-specific patterns last
    # so they win over the generic BANK_CHECKING fallback.
    op.execute(
        """
        UPDATE accounts
        SET account_kind = 'BANK_CHECKING'
        WHERE account_type = 'ASSET'
          AND bsb IS NOT NULL
          AND account_kind IS NULL
        """
    )
    op.execute(
        """
        UPDATE accounts
        SET account_kind = 'BANK_SAVINGS'
        WHERE account_type = 'ASSET'
          AND lower(name) LIKE '%savings%'
        """
    )
    op.execute(
        """
        UPDATE accounts
        SET account_kind = 'CASH'
        WHERE account_type = 'ASSET'
          AND (lower(name) LIKE '%petty cash%' OR lower(name) ~ '\\mcash\\M')
          AND account_kind IS NULL
        """
    )
    op.execute(
        """
        UPDATE accounts
        SET account_kind = 'CREDIT_CARD'
        WHERE account_type = 'LIABILITY'
          AND (lower(name) LIKE '%credit card%' OR lower(name) LIKE '%card%')
        """
    )
    op.execute(
        """
        UPDATE accounts
        SET account_kind = 'BANK_LOAN'
        WHERE account_type = 'LIABILITY'
          AND (lower(name) LIKE '%loan%' OR lower(name) LIKE '%mortgage%')
        """
    )


def downgrade() -> None:
    op.drop_index("ix_accounts_account_kind", table_name="accounts")
    op.drop_column("accounts", "account_kind")
    postgresql.ENUM(*ACCOUNT_KINDS, name="account_kind_enum").drop(
        op.get_bind(), checkfirst=True
    )
