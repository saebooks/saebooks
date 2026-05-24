"""0133_entity_types — add entity_type / trades / trustee_company_id to companies.

Captures the legal-entity shape needed to distinguish:
  - COMPANY (trading) — ABN + ACN
  - COMPANY (trustee-only, doesn't trade) — ACN only, no ABN
  - TRUST — ABN, no ACN, trustee_company_id set
  - INDIVIDUAL (natural person)
  - PARTNERSHIP / SUPER_FUND (future)

Forward-compatible: existing rows get entity_type='COMPANY' + trades=true, which
matches their effective behaviour. Operator can refine after the fact.

Revision ID: 0133_entity_types
Revises: 0132_gst_system_managed_backfill
Create Date: 2026-05-24
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0133_entity_types"
down_revision: str | None = "0132_gst_system_managed_backfill"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


ENTITY_TYPES = ("COMPANY", "TRUST", "INDIVIDUAL", "PARTNERSHIP", "SUPER_FUND")


def upgrade() -> None:
    # Enum
    entity_type = sa.Enum(*ENTITY_TYPES, name="entity_type_enum")
    entity_type.create(op.get_bind(), checkfirst=True)

    # entity_type — NOT NULL with default
    op.add_column(
        "companies",
        sa.Column(
            "entity_type",
            sa.Enum(*ENTITY_TYPES, name="entity_type_enum", create_type=False),
            nullable=False,
            server_default="COMPANY",
        ),
    )

    # trades — NOT NULL default true
    op.add_column(
        "companies",
        sa.Column("trades", sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    # trustee_company_id — nullable self-FK
    op.add_column(
        "companies",
        sa.Column("trustee_company_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "companies_trustee_company_id_fkey",
        "companies",
        "companies",
        ["trustee_company_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_companies_trustee_company_id",
        "companies",
        ["trustee_company_id"],
    )

    # Cohesion CHECK: a Trust SHOULD have trustee_company_id set; a non-trust
    # SHOULD NOT. Implemented as a soft assertion so existing rows pass (they
    # default to COMPANY + null trustee, satisfying both legs).
    op.execute("""
        ALTER TABLE companies
        ADD CONSTRAINT ck_trustee_only_on_trust
        CHECK (
          (entity_type = 'TRUST') OR (trustee_company_id IS NULL)
        )
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE companies DROP CONSTRAINT IF EXISTS ck_trustee_only_on_trust")
    op.drop_index("ix_companies_trustee_company_id", table_name="companies")
    op.drop_constraint("companies_trustee_company_id_fkey", "companies", type_="foreignkey")
    op.drop_column("companies", "trustee_company_id")
    op.drop_column("companies", "trades")
    op.drop_column("companies", "entity_type")
    sa.Enum(name="entity_type_enum").drop(op.get_bind(), checkfirst=True)
