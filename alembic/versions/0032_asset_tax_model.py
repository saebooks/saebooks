"""Tax-vs-book depreciation split (Batch NN)

Adds a nullable ``tax_model_id`` FK to ``fixed_assets``. The existing
``depreciation_model_id`` column stays — it's the **book** model now
(management-accounting cadence, typically straight-line). When
``tax_model_id`` is NULL the tax and book schedules are identical
(most SMBs with no compliance divergence). When set, it points at a
second ``depreciation_models`` row — typically a DV rate matching
the ATO effective life for the asset class — and the tax schedule
diverges from the book schedule.

The tax schedule is **off-GL** — we don't post tax-only depreciation
journals. It's a reporting-only overlay: the `/reports/depreciation-schedule`
page shows book vs tax cumulative side-by-side so the BAS preparer
can compute the temporary tax-vs-book difference for deferred tax
disclosure.

Not introducing an `asset_tax_depreciation` postings table here —
tax depreciation is recomputed on-demand from
``(cost, residual, in_service_date, tax_model_id)`` the same way
book depreciation is. Adding a materialised schedule table can wait
until a real customer hits a performance wall (unlikely for a few
thousand assets).

Additive migration — no backfill. Pre-existing rows get
``tax_model_id IS NULL`` which means "tax == book".

Revision ID: 0032_asset_tax_model
Revises: 0031_asset_partial_disposal
Create Date: 2026-04-21
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0032_asset_tax_model"
down_revision: str | None = "0031_asset_partial_disposal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "fixed_assets",
        sa.Column(
            "tax_model_id",
            sa.String(64),
            sa.ForeignKey("depreciation_models.id", ondelete="RESTRICT"),
            nullable=True,
            comment=(
                "Optional tax-depreciation model (e.g. asset_dv_30). NULL "
                "means the tax schedule matches the book schedule — most "
                "SMBs without compliance divergence stay on NULL. When "
                "set, the /reports/depreciation-schedule view shows book "
                "vs tax side-by-side for deferred-tax disclosure."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("fixed_assets", "tax_model_id")
