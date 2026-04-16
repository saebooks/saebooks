"""bank feed connections for SISS integration

Adds three tables (bank_feed_clients, bank_feed_accounts,
bank_feed_issues) that sit between saebooks' companies/accounts rows
and the upstream aggregator's client/account/issue identifiers, plus
an ``external_id`` column on ``bank_statement_lines`` to make
incremental transaction sync idempotent.

See ``saebooks/services/bank_feeds/`` for the module that reads/writes
these tables.

Revision ID: 0016_bank_feed_connections
Revises: 0015_sql_queries
Create Date: 2026-04-17
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0016_bank_feed_connections"
down_revision: str | None = "0015_sql_queries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---------------------------------------------------------------- #
    # bank_feed_clients — one row per company registered upstream       #
    # ---------------------------------------------------------------- #
    op.create_table(
        "bank_feed_clients",
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
            unique=True,
            comment="1:1 with companies — one aggregator client per company",
        ),
        sa.Column(
            "sds_client_id",
            sa.String(128),
            nullable=False,
            unique=True,
            comment="The aggregator's opaque client identifier",
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "last_sync_at",
            sa.DateTime(timezone=True),
            comment="When the most recent successful sync completed",
        ),
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
    )

    # ---------------------------------------------------------------- #
    # bank_feed_accounts — one per connected upstream account           #
    # ---------------------------------------------------------------- #
    op.create_table(
        "bank_feed_accounts",
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
            "bank_feed_client_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bank_feed_clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ledger_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
            comment="The chart-of-accounts row this feed writes statement lines against",
        ),
        sa.Column(
            "sds_account_id",
            sa.String(128),
            nullable=False,
            unique=True,
            comment="The aggregator's opaque account identifier",
        ),
        sa.Column(
            "sds_institution_id",
            sa.String(128),
            nullable=False,
        ),
        sa.Column("masked_number", sa.String(64)),
        sa.Column("display_name", sa.String(255)),
        sa.Column(
            "product_category",
            sa.String(64),
            comment="Standard CDR-Banking productCategory enum value",
        ),
        sa.Column(
            "feed_type",
            sa.String(32),
            comment="DIRECT_FEED | BUSINESS_DISCLOSURE | TRUSTED_ADVISER | PENDING",
        ),
        sa.Column(
            "processing_status",
            sa.String(4),
            comment="Aggregator processing status flag (single-letter code)",
        ),
        sa.Column("processing_status_date", sa.Date()),
        sa.Column(
            "last_transaction_posted_id",
            sa.String(128),
            comment="Canonical cursor for incremental transaction sync",
        ),
        sa.Column("last_transaction_posted_date", sa.Date()),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            comment="Set when the feed has been revoked upstream; row is soft-deleted",
        ),
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
    )
    op.create_index(
        "ix_bank_feed_accounts_company_id",
        "bank_feed_accounts",
        ["company_id"],
    )
    op.create_index(
        "ix_bank_feed_accounts_ledger_account_id",
        "bank_feed_accounts",
        ["ledger_account_id"],
    )
    op.create_index(
        "ix_bank_feed_accounts_client_id",
        "bank_feed_accounts",
        ["bank_feed_client_id"],
    )

    # ---------------------------------------------------------------- #
    # bank_feed_issues — cache of upstream feed-health issues           #
    # ---------------------------------------------------------------- #
    op.create_table(
        "bank_feed_issues",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "sds_feed_issue_id",
            sa.String(128),
            nullable=False,
            unique=True,
        ),
        sa.Column("sds_institution_id", sa.String(128), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            comment="active | closed",
        ),
        sa.Column("creation_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_datetime", sa.DateTime(timezone=True)),
        sa.Column("last_message", sa.Text()),
        sa.Column("last_update_datetime", sa.DateTime(timezone=True)),
        sa.Column("country", sa.String(8)),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="When this row was last refreshed from the aggregator",
        ),
    )
    op.create_index(
        "ix_bank_feed_issues_institution_status",
        "bank_feed_issues",
        ["sds_institution_id", "status"],
    )

    # ---------------------------------------------------------------- #
    # bank_statement_lines — add external_id for idempotent upserts    #
    # ---------------------------------------------------------------- #
    op.add_column(
        "bank_statement_lines",
        sa.Column(
            "external_id",
            sa.String(255),
            comment="Upstream transactionId — used to dedupe on resync",
        ),
    )
    op.add_column(
        "bank_statement_lines",
        sa.Column(
            "bank_feed_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bank_feed_accounts.id", ondelete="SET NULL"),
            comment="Source feed account, if this line came from a bank feed",
        ),
    )
    # Partial unique index — null external_ids (manually-entered lines)
    # remain unconstrained, but any feed-ingested line is deduped on
    # (feed_account, external_id).
    op.create_index(
        "ux_bank_statement_lines_feed_external",
        "bank_statement_lines",
        ["bank_feed_account_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ux_bank_statement_lines_feed_external", table_name="bank_statement_lines"
    )
    op.drop_column("bank_statement_lines", "bank_feed_account_id")
    op.drop_column("bank_statement_lines", "external_id")

    op.drop_index(
        "ix_bank_feed_issues_institution_status", table_name="bank_feed_issues"
    )
    op.drop_table("bank_feed_issues")

    op.drop_index(
        "ix_bank_feed_accounts_client_id", table_name="bank_feed_accounts"
    )
    op.drop_index(
        "ix_bank_feed_accounts_ledger_account_id", table_name="bank_feed_accounts"
    )
    op.drop_index(
        "ix_bank_feed_accounts_company_id", table_name="bank_feed_accounts"
    )
    op.drop_table("bank_feed_accounts")

    op.drop_table("bank_feed_clients")
