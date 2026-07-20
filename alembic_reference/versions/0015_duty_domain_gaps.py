"""Duties-domain reference gaps (M1.5 · Wave 5-DUTIES).

Purely additive, following the T3/T4 pattern (new reference tables +
nullable columns + AU seed; no posting-path change):

* ``duty_surcharge_rates`` — foreign / non-resident purchaser surcharge
  duty catalog (QLD AFAD, NSW Surcharge Purchaser Duty, VIC FPAD, ...),
  effective-dated.
* ``landholder_duty_rules`` — landholder / land-rich entity indirect
  transfer duty trigger rules (landholding threshold + significant
  interest %), effective-dated.
* ``securities_duty_rates`` — securities-transfer duty / share-FTT rate
  catalog, a sibling of ``duty_rate_schedules`` per the audit. The AU
  seed rows are all CLOSED (Australia abolished marketable-securities
  duty) — AU parity is "no open row".
* ``lease_duty_rates`` — lease / tenancy agreement duty rate catalog.
  AU seed rows likewise all CLOSED (rent-based lease duty abolished in
  the 2000s).
* ``duty_rate_schedules`` gains NULLABLE ``effective_from`` /
  ``effective_to`` (existing rows keep undated open-ended semantics)
  plus the natural-key uniqueness the audit flagged as missing
  (jurisdiction, state, transaction_type, lower_bound, effective_from —
  only bites dated series; Postgres treats NULLs as distinct), and its
  ``transaction_type`` comment learns the securities / lease /
  landholder_acquisition vocabulary added to ``DutyType``.

Reversible: downgrade drops the four tables, the two columns, the
constraint, and restores the old comment.

Revision ID: 0014_duty_domain_gaps
Revises: 0013_coa_statutory_frameworks
Create Date: 2026-07-12
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0015_duty_domain_gaps"
down_revision: str | None = "0014_income_tax_reference"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_OLD_TXN_TYPE_COMMENT = "property_transfer | motor_vehicle | insurance | mortgage"
_NEW_TXN_TYPE_COMMENT = (
    "property_transfer | motor_vehicle | insurance | mortgage | "
    "securities | lease | landholder_acquisition"
)


def upgrade() -> None:
    op.create_table(
        "duty_surcharge_rates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "jurisdiction",
            sa.String(3),
            sa.ForeignKey("jurisdictions.code"),
            nullable=False,
        ),
        sa.Column("sub_jurisdiction", sa.String(8), nullable=False),
        sa.Column("transaction_type", sa.String(64), nullable=False),
        sa.Column("purchaser_class", sa.String(32), nullable=False),
        sa.Column("surcharge_rate", sa.Numeric(7, 4), nullable=False),
        sa.Column("land_use_scope", sa.String(32)),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date()),
        sa.Column("description", sa.String(512)),
        sa.UniqueConstraint(
            "jurisdiction", "sub_jurisdiction", "transaction_type",
            "purchaser_class", "effective_from",
            name="uq_duty_surcharge_rates_natkey",
        ),
    )

    op.create_table(
        "landholder_duty_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "jurisdiction",
            sa.String(3),
            sa.ForeignKey("jurisdictions.code"),
            nullable=False,
        ),
        sa.Column("sub_jurisdiction", sa.String(8), nullable=False),
        sa.Column("entity_class", sa.String(32), nullable=False),
        sa.Column("landholding_value_threshold", sa.Numeric(14, 2), nullable=False),
        sa.Column("significant_interest_pct", sa.Numeric(5, 2), nullable=False),
        sa.Column("duty_basis", sa.String(32), nullable=False),
        sa.Column("basis_fraction", sa.Numeric(7, 4)),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date()),
        sa.Column("description", sa.String(512)),
        sa.UniqueConstraint(
            "jurisdiction", "sub_jurisdiction", "entity_class", "effective_from",
            name="uq_landholder_duty_rules_natkey",
        ),
    )

    op.create_table(
        "securities_duty_rates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "jurisdiction",
            sa.String(3),
            sa.ForeignKey("jurisdictions.code"),
            nullable=False,
        ),
        sa.Column("sub_jurisdiction", sa.String(8), nullable=False),
        sa.Column("security_class", sa.String(16), nullable=False),
        sa.Column("rate", sa.Numeric(7, 4), nullable=False),
        sa.Column("rate_basis", sa.String(32), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date()),
        sa.Column("description", sa.String(512)),
        sa.UniqueConstraint(
            "jurisdiction", "sub_jurisdiction", "security_class", "effective_from",
            name="uq_securities_duty_rates_natkey",
        ),
    )

    op.create_table(
        "lease_duty_rates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "jurisdiction",
            sa.String(3),
            sa.ForeignKey("jurisdictions.code"),
            nullable=False,
        ),
        sa.Column("sub_jurisdiction", sa.String(8), nullable=False),
        sa.Column("duty_base", sa.String(32), nullable=False),
        sa.Column("rate", sa.Numeric(7, 4), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date()),
        sa.Column("description", sa.String(512)),
        sa.UniqueConstraint(
            "jurisdiction", "sub_jurisdiction", "duty_base", "effective_from",
            name="uq_lease_duty_rates_natkey",
        ),
    )

    op.add_column(
        "duty_rate_schedules",
        sa.Column("effective_from", sa.Date(), nullable=True),
    )
    op.add_column(
        "duty_rate_schedules",
        sa.Column("effective_to", sa.Date(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_duty_rate_schedules_natkey",
        "duty_rate_schedules",
        ["jurisdiction", "state", "transaction_type", "lower_bound",
         "effective_from"],
    )
    op.alter_column(
        "duty_rate_schedules",
        "transaction_type",
        existing_type=sa.String(64),
        existing_nullable=False,
        comment=_NEW_TXN_TYPE_COMMENT,
        existing_comment=_OLD_TXN_TYPE_COMMENT,
    )


def downgrade() -> None:
    op.alter_column(
        "duty_rate_schedules",
        "transaction_type",
        existing_type=sa.String(64),
        existing_nullable=False,
        comment=_OLD_TXN_TYPE_COMMENT,
        existing_comment=_NEW_TXN_TYPE_COMMENT,
    )
    op.drop_constraint(
        "uq_duty_rate_schedules_natkey", "duty_rate_schedules", type_="unique"
    )
    op.drop_column("duty_rate_schedules", "effective_to")
    op.drop_column("duty_rate_schedules", "effective_from")
    op.drop_table("lease_duty_rates")
    op.drop_table("securities_duty_rates")
    op.drop_table("landholder_duty_rules")
    op.drop_table("duty_surcharge_rates")
