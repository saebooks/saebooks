"""Add signup_plan column to users.

Persists the plan the user selected on the marketing site CTA
(business / pro / enterprise) across the email verification delay.
After email verification completes, the web layer reads this value,
clears it, and redirects the user to /billing/checkout?plan=<plan>.

The column is nullable (NULL = community / no paid plan selected).
A CHECK constraint keeps the set of valid values in sync with what
the Stripe Checkout session handler accepts.

Revision ID: 0079_user_signup_plan
Revises: 0078_tenant_edition
Create Date: 2026-04-29
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0079_user_signup_plan"
down_revision: str | None = "0078_tenant_edition"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("signup_plan", sa.String(16), nullable=True),
    )
    op.create_check_constraint(
        "ck_users_signup_plan_valid",
        "users",
        "signup_plan IS NULL OR signup_plan IN ('business','pro','enterprise')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_signup_plan_valid", "users", type_="check")
    op.drop_column("users", "signup_plan")
