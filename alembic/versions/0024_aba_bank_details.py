"""Bank details for ABA export

Revision ID: 0024_aba_bank_details
Revises: 0023_bill_allocations
Create Date: 2026-04-21

Adds the fields needed to generate APCA / ABA (Direct Entry) bank
files for bulk supplier payments:

* ``accounts`` (bank accounts only) — gains ``bsb`` (``123-456``),
  ``bank_account_number`` (up to 9 chars), ``bank_account_title``
  (32 chars, goes in the remitter block of the ABA header), plus
  ``apca_user_id`` (6-digit Direct Entry User ID issued by the
  sponsor bank) and ``bank_abbreviation`` (``CBA``/``ANZ``/``NAB``/
  ``WBC`` — three-letter code that goes in the ABA header).

  The ABA sponsor agreement is per bank account (you can have two
  bank accounts with two different APCA IDs at different banks), so
  these columns live on the ledger ``accounts`` row, not on the
  company. Non-bank accounts leave them null — nothing breaks.

* ``contacts`` (payee side) — gains ``bank_bsb``,
  ``bank_account_number`` and ``bank_account_title`` for the payee's
  BSB + account + name-on-account. These are also the fields shown
  on a remittance advice.

Nothing in the existing code touches these columns yet; the ABA
service and pay-run UI in Batch W are the first consumers.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024_aba_bank_details"
down_revision: str | None = "0023_bill_allocations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- accounts (remitter side) ---------------------------------------
    op.add_column(
        "accounts",
        sa.Column("bsb", sa.String(7), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column("bank_account_number", sa.String(9), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column("bank_account_title", sa.String(32), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column("apca_user_id", sa.String(6), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column("bank_abbreviation", sa.String(3), nullable=True),
    )

    # --- contacts (payee side) ------------------------------------------
    op.add_column(
        "contacts",
        sa.Column("bank_bsb", sa.String(7), nullable=True),
    )
    op.add_column(
        "contacts",
        sa.Column("bank_account_number", sa.String(9), nullable=True),
    )
    op.add_column(
        "contacts",
        sa.Column("bank_account_title", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("contacts", "bank_account_title")
    op.drop_column("contacts", "bank_account_number")
    op.drop_column("contacts", "bank_bsb")

    op.drop_column("accounts", "bank_abbreviation")
    op.drop_column("accounts", "apca_user_id")
    op.drop_column("accounts", "bank_account_title")
    op.drop_column("accounts", "bank_account_number")
    op.drop_column("accounts", "bsb")
