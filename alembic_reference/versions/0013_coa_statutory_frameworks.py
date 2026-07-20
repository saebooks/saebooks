"""CoA statutory-framework reference tables (M1.5 · Wave 5-CoA / T10b).

Purely additive, following the T3/T4 pattern (new reference tables +
nullable columns + AU seed; no posting-path change):

* ``statutory_account_frameworks`` — registry of jurisdiction-mandated (or
  conventional) chart-of-accounts numbering plans (SKR03/SKR04, PCG, ...).
  Australia mandates none, so the AU seed row carries
  ``is_legally_mandated = false`` and AU behaviour is unchanged.
* ``reporting_taxonomies`` — registry of regulator e-filing /
  financial-statement taxonomies (AU SBR, UK iXBRL, EU ESEF, ...). The AU
  seed row names the SBR AU taxonomy the live lodgement path already
  renders XBRL under.
* ``chart_template`` gains four nullable statutory-mapping columns
  (framework code, statutory account number, local label, parent class).
  Existing AU rows stay NULL — the AU recommended chart is a convention,
  not a mandate.

Reversible: downgrade drops the two tables and the four columns.

Revision ID: 0013_coa_statutory_frameworks
Revises: 0012_reference_rename_sweep
Create Date: 2026-07-12
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0013_coa_statutory_frameworks"
down_revision: str | None = "0012_reference_rename_sweep"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_CHART_COLS = (
    "statutory_framework_code",
    "statutory_account_code",
    "statutory_account_label_local",
    "statutory_parent_class",
)


def upgrade() -> None:
    op.create_table(
        "statutory_account_frameworks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "jurisdiction",
            sa.String(3),
            sa.ForeignKey("jurisdictions.code"),
            nullable=False,
        ),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column(
            "is_legally_mandated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("mandating_authority", sa.String(128)),
        sa.Column("version", sa.String(32)),
        sa.Column("description", sa.String(512)),
        sa.UniqueConstraint(
            "jurisdiction", "code",
            name="uq_statutory_account_frameworks_jur_code",
        ),
    )

    op.create_table(
        "reporting_taxonomies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "jurisdiction",
            sa.String(3),
            sa.ForeignKey("jurisdictions.code"),
            nullable=False,
        ),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("taxonomy_format", sa.String(16), nullable=False),
        sa.Column("authority", sa.String(128)),
        sa.Column("version", sa.String(64)),
        sa.Column("description", sa.String(512)),
        sa.UniqueConstraint(
            "jurisdiction", "code",
            name="uq_reporting_taxonomies_jur_code",
        ),
    )

    op.add_column(
        "chart_template",
        sa.Column("statutory_framework_code", sa.String(32), nullable=True),
    )
    op.add_column(
        "chart_template",
        sa.Column("statutory_account_code", sa.String(32), nullable=True),
    )
    op.add_column(
        "chart_template",
        sa.Column("statutory_account_label_local", sa.String(255), nullable=True),
    )
    op.add_column(
        "chart_template",
        sa.Column("statutory_parent_class", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    for col in reversed(_CHART_COLS):
        op.drop_column("chart_template", col)
    op.drop_table("reporting_taxonomies")
    op.drop_table("statutory_account_frameworks")
