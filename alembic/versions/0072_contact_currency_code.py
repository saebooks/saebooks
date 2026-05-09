"""Add currency_code to contacts for foreign-supplier billing currency.

Gap ETSY-2 (micro-etsy-reseller): contacts with country != Australia had no
billing currency field, causing bills to default to AUD face-value entry with
no FX prompt. This column stores the preferred ISO 4217 billing currency so
bill creation can default to the supplier's currency and offer an FX rate input.

Revision ID: 0072_contact_currency_code
Revises: 0071_trade_in
Create Date: 2026-04-29
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0072_contact_currency_code"
down_revision: str | None = "0071_trade_in"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "contacts",
        sa.Column(
            "currency_code",
            sa.String(3),
            nullable=True,
            comment="ISO 4217 billing currency, e.g. JPY, USD. NULL implies AUD.",
        ),
    )


def downgrade() -> None:
    op.drop_column("contacts", "currency_code")
