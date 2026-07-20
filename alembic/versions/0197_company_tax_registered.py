"""Rename Company.gst_registered -> tax_registered (M1.5, Wave 3b, Class B).

Pure value-preserving column rename. ``companies.gst_registered`` (bool,
created migration ``0065_gst_fields_on_companies``) is consumed by live
reports/cashbook/companies/mcp code as "is this company registered for the
jurisdiction's home consumption tax". AU semantics are unchanged by this
rename: ``tax_registered=true`` still means "registered for GST" for an AU
company — it is only the column/field name that generalises away from the
AU-specific "gst" term, per the M1.5 rename-hygiene audit (Deliverable 2,
Class B). ``gst_effective_date`` is intentionally left as-is — out of scope
for this pure bool rename (see plan discussion point 3).

``op.alter_column(new_column_name=...)`` preserves the existing data,
default, and NOT NULL constraint — no data loss, no value transform, no
new nullability. Fully reversible: downgrade renames the column back.

Chains off the current company-DB head ``0196_ee_filing_ref_cols`` (verified
via ``alembic heads``). Disjoint from the reference-tree migrations
(``alembic_reference/``) and from the Class A reference-hygiene migration
running in parallel on a different tree/head.

Revision ID: 0197_company_gst_registered_to_tax_registered
Revises:     0196_ee_filing_ref_cols
Create Date: 2026-07-11
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0197_company_tax_registered"
down_revision: str | None = "0196_ee_filing_ref_cols"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLE = "companies"


def upgrade() -> None:
    op.alter_column(
        _TABLE,
        "gst_registered",
        new_column_name="tax_registered",
        existing_type=sa.Boolean(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        _TABLE,
        "tax_registered",
        new_column_name="gst_registered",
        existing_type=sa.Boolean(),
        existing_nullable=False,
    )
