"""Add edition + Stripe correlation columns to tenants.

The Stripe webhook minted from ``checkout.session.completed`` writes
``stripe_customer_id`` + ``stripe_subscription_id`` so subsequent
``customer.subscription.updated/deleted`` events can locate the
tenant without going via the user's email (the email may have
changed since signup).

``edition`` ladder: community ⊂ business ⊂ pro ⊂ enterprise. Strict
superset semantics already encoded in services/features.py — the
column gives us a per-tenant override of the runtime
``SAEBOOKS_EDITION`` env so different tenants on the same instance
can hold different paid tiers.

Revision ID: 0078_tenant_edition
Revises: 0077_user_auth_tokens
Create Date: 2026-04-29
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0078_tenant_edition"
down_revision: str | None = "0077_user_auth_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "edition",
            sa.String(16),
            nullable=False,
            server_default="community",
        ),
    )
    op.create_check_constraint(
        "ck_tenants_edition_valid",
        "tenants",
        "edition IN ('community','business','pro','enterprise')",
    )
    op.add_column(
        "tenants",
        sa.Column("stripe_customer_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("stripe_subscription_id", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_tenants_stripe_customer_id",
        "tenants",
        ["stripe_customer_id"],
        unique=False,
        postgresql_where=sa.text("stripe_customer_id IS NOT NULL"),
    )
    op.create_index(
        "ix_tenants_stripe_subscription_id",
        "tenants",
        ["stripe_subscription_id"],
        unique=False,
        postgresql_where=sa.text("stripe_subscription_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_tenants_stripe_subscription_id", table_name="tenants")
    op.drop_index("ix_tenants_stripe_customer_id", table_name="tenants")
    op.drop_column("tenants", "stripe_subscription_id")
    op.drop_column("tenants", "stripe_customer_id")
    op.drop_constraint("ck_tenants_edition_valid", "tenants", type_="check")
    op.drop_column("tenants", "edition")
