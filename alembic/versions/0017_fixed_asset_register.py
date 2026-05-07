"""fixed asset register + depreciation model catalogue

Adds two tables for the fixed-asset register:

1. ``depreciation_models`` — jurisdiction-level catalogue of
   depreciation schedules. Seeded from
   ``saebooks/seed/au/account.depreciation.model-au.csv`` (6 rows for
   Australia: no-depreciation plus linear 3/4/5/10/20 year).

2. ``fixed_assets`` — the register itself. Combines accounting fields
   (cost, accum-dep, dep-expense accounts, depreciation model, cost
   basis, residual) with physical-tracking fields (serial, location,
   custody, warranty). Per-company; cascade-deletes with the company.

See ``saebooks/services/assets.py`` for the business logic that
reads/writes these tables.

Revision ID: 0017_fixed_asset_register
Revises: 0016_bank_feed_connections
Create Date: 2026-04-20
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0017_fixed_asset_register"
down_revision: str | None = "0016_bank_feed_connections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---------------------------------------------------------------- #
    # depreciation_models — jurisdiction-level catalogue               #
    # ---------------------------------------------------------------- #
    op.create_table(
        "depreciation_models",
        sa.Column(
            "id",
            sa.String(64),
            primary_key=True,
            comment="Slug like 'asset_5_year_linear' — stable across installs",
        ),
        sa.Column(
            "method",
            sa.String(32),
            nullable=False,
            comment="'no_depreciation' | 'linear' — more methods land here later",
        ),
        sa.Column(
            "method_number",
            sa.Integer(),
            nullable=False,
            comment="Years of useful life for linear; 0 for no_depreciation",
        ),
        sa.Column(
            "method_period",
            sa.Integer(),
            nullable=False,
            comment="Periods per method_number unit — 12 = monthly over N years",
        ),
        sa.Column(
            "method_progress_factor",
            sa.Numeric(8, 4),
            comment="Reserved for diminishing-value rate",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # ---------------------------------------------------------------- #
    # fixed_assets — the register                                       #
    # ---------------------------------------------------------------- #
    op.create_table(
        "fixed_assets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "code",
            sa.String(32),
            nullable=False,
            comment="Short identifier e.g. FA-0001",
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text()),
        # GL coordinates
        sa.Column(
            "cost_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
            comment="The cost account (e.g. 1-3310 Office Equipment)",
        ),
        sa.Column(
            "accum_dep_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
            comment="Paired accumulated-depreciation contra-asset account",
        ),
        sa.Column(
            "dep_expense_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
            comment="P&L account to debit on depreciation (default 6-1500)",
        ),
        sa.Column(
            "depreciation_model_id",
            sa.String(64),
            sa.ForeignKey("depreciation_models.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # Money / dates
        sa.Column("purchase_date", sa.Date(), nullable=False),
        sa.Column(
            "in_service_date",
            sa.Date(),
            nullable=False,
            comment="Depreciation starts from here",
        ),
        sa.Column("cost", sa.Numeric(18, 2), nullable=False),
        sa.Column(
            "residual_value",
            sa.Numeric(18, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "last_depreciation_posted_through",
            sa.Date(),
            comment="Idempotency cursor; NULL until first depreciation run",
        ),
        # Lifecycle
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="active",
            comment="active | disposed | archived",
        ),
        sa.Column("disposal_date", sa.Date()),
        sa.Column("disposal_proceeds", sa.Numeric(18, 2)),
        sa.Column(
            "disposal_journal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("journal_entries.id", ondelete="SET NULL"),
        ),
        # Physical tracking
        sa.Column("serial_number", sa.String()),
        sa.Column("manufacturer", sa.String()),
        sa.Column("model_number", sa.String()),
        sa.Column("location", sa.String()),
        sa.Column(
            "custody_person",
            sa.String(),
            comment="Free-text for v1; upgrade to FK when employees model lands",
        ),
        sa.Column("warranty_end", sa.Date()),
        sa.Column(
            "purchase_contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"),
        ),
        # Unstructured + audit
        sa.Column("extra", postgresql.JSONB()),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "company_id", "code", name="uq_fixed_assets_company_code"
        ),
    )
    op.create_index(
        "ix_fixed_assets_company_status",
        "fixed_assets",
        ["company_id", "status"],
    )
    op.create_index(
        "ix_fixed_assets_company_archived",
        "fixed_assets",
        ["company_id", "archived_at"],
    )

    # ---------------------------------------------------------------- #
    # Seed gain/loss-on-disposal accounts                              #
    # ---------------------------------------------------------------- #
    # These two accounts aren't in the bulk CoA seed; depreciation
    # disposal needs them. Inserted here rather than in the CSV so an
    # upgrading instance gets them without re-seeding. `ON CONFLICT DO
    # NOTHING` keeps the migration safe if someone already added them
    # manually.
    op.execute(
        sa.text(
            """
            INSERT INTO accounts (
                id, company_id, code, name, account_type, created_at
            )
            SELECT
                gen_random_uuid(),
                c.id,
                '4-9100',
                'Gain on Disposal of Assets',
                'OTHER_INCOME',
                now()
            FROM companies c
            WHERE NOT EXISTS (
                SELECT 1 FROM accounts a
                WHERE a.company_id = c.id AND a.code = '4-9100'
            );
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO accounts (
                id, company_id, code, name, account_type, created_at
            )
            SELECT
                gen_random_uuid(),
                c.id,
                '6-9100',
                'Loss on Disposal of Assets',
                'EXPENSE',
                now()
            FROM companies c
            WHERE NOT EXISTS (
                SELECT 1 FROM accounts a
                WHERE a.company_id = c.id AND a.code = '6-9100'
            );
            """
        )
    )


def downgrade() -> None:
    # Leave the 4-9100 / 6-9100 accounts in place — removing them here
    # would fail any ON DELETE RESTRICT references from posted disposal
    # journals. Admins can archive them manually if they need to.

    op.drop_index("ix_fixed_assets_company_archived", table_name="fixed_assets")
    op.drop_index("ix_fixed_assets_company_status", table_name="fixed_assets")
    op.drop_table("fixed_assets")
    op.drop_table("depreciation_models")
