"""0165_tax_code_jurisdiction — add jurisdiction tag to tax_codes + seed
the curated international reference tax-code set.

Why this migration exists
-------------------------
The tax engine is already per-jurisdiction (``services/tax_engine/au.py``,
with NZ/UK/EE stubs registered). The ``tax_codes`` table, however, had no
jurisdiction column and a partial-unique index on ``(company_id, code)``
among active rows — so it could hold AU "GST" but never a second "GST"
for NZ in the same company.

This migration:

1. Adds ``tax_codes.jurisdiction`` (varchar(2-8), NOT NULL, default 'AU').
   Every existing row is AU, so the backfill is a single UPDATE.

2. Replaces the partial-unique index
   ``uq_tax_codes_company_code_active`` ((company_id, code) WHERE
   archived_at IS NULL) with
   ``uq_tax_codes_company_jurisdiction_code_active``
   ((company_id, jurisdiction, code) WHERE archived_at IS NULL). This is
   strictly LOOSER than the old index for existing data (all rows are
   jurisdiction='AU', so the old uniqueness is preserved within AU) and
   lets AU "GST" and NZ "GST" coexist.

The international reference codes themselves are inserted by the seed
function ``saebooks.services.tax_codes.ensure_international_seed`` (called
from the seed CLIs / app bootstrap), NOT by this migration — the data seed
is idempotent and company-scoped, and a schema migration must not depend on
which companies exist. This migration only makes the schema able to hold
them.

Reversibility
-------------
``downgrade`` drops the new index, recreates the old one, and drops the
column. It does NOT delete any seeded international rows — if international
codes were seeded, the old ``(company_id, code)`` unique index could fail
to recreate on downgrade (two rows would share a code). The downgrade
therefore archives any non-AU active rows first so the old index can be
rebuilt cleanly. This is the expected behaviour: downgrading removes the
multi-jurisdiction capability, so the international codes can no longer be
represented.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0165_tax_code_jurisdiction"
down_revision: str | None = "0164_je_guard_fixes"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # 1. Add the column nullable first so we can backfill, then enforce
    #    NOT NULL with a server-side default for future inserts.
    op.add_column(
        "tax_codes",
        sa.Column("jurisdiction", sa.String(8), nullable=True),
    )
    op.execute("UPDATE tax_codes SET jurisdiction = 'AU' WHERE jurisdiction IS NULL")
    op.alter_column(
        "tax_codes",
        "jurisdiction",
        existing_type=sa.String(8),
        nullable=False,
        server_default="AU",
    )

    # 2. Swap the partial-unique index to include jurisdiction.
    op.drop_index("uq_tax_codes_company_code_active", table_name="tax_codes")
    op.create_index(
        "uq_tax_codes_company_jurisdiction_code_active",
        "tax_codes",
        ["company_id", "jurisdiction", "code"],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL"),
    )
    # Helpful non-unique index for jurisdiction-filtered lists.
    op.create_index(
        "ix_tax_codes_company_jurisdiction",
        "tax_codes",
        ["company_id", "jurisdiction"],
    )


def downgrade() -> None:
    # Archive any active non-AU rows so the old (company_id, code) unique
    # index can be recreated without collision (e.g. AU "GST" + NZ "GST").
    op.execute(
        "UPDATE tax_codes SET archived_at = now() "
        "WHERE jurisdiction <> 'AU' AND archived_at IS NULL"
    )
    op.drop_index(
        "ix_tax_codes_company_jurisdiction", table_name="tax_codes"
    )
    op.drop_index(
        "uq_tax_codes_company_jurisdiction_code_active", table_name="tax_codes"
    )
    op.create_index(
        "uq_tax_codes_company_code_active",
        "tax_codes",
        ["company_id", "code"],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL"),
    )
    op.drop_column("tax_codes", "jurisdiction")
