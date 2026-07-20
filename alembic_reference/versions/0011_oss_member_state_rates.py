"""oss_member_state_rates reference table (EE-frontier scope, Module 2 — OSS-Q).

Per-EU-member-state standard VAT rate, needed to compute the destination-
country VAT payable under the Union OSS scheme (``tax_return_generator``'s
box-vector engine has no shape for this: OSS-Q is a per-(member-state x
rate) repeating listing, not a fixed box vector — see
``services.lodgement.oss_q.generator``'s module docstring). Mirrors
``duty_concessions`` (0006): a plain reference table, NOT company-scoped,
FK'd to the existing ``countries`` table (reused, not duplicated — only
``countries.code`` rows already carrying ``in_oss=true`` are eligible
destinations).

Scope note (flagged, not silently narrowed): ``countries.yaml`` today only
seeds 18 EU member states with ``in_oss: true`` (the file's own header:
"DE/FR/IT/ES are EU shells that fill in at v0.1.9" plus the ones added
since). This migration seeds the DESTINATION rates for the 17 of those
that are not Estonia itself (OSS reports CROSS-BORDER B2C supply, so
Estonia is never its own OSS destination). The other 9 currently-EU
member states (Croatia, Slovenia, Slovakia, Hungary, Romania, Bulgaria,
Greece, Cyprus, Malta) have no ``countries`` row yet, so they cannot be
FK-targeted or seeded here — that is a follow-up (extend
``_global/countries.yaml`` first), named explicitly rather than silently
omitted.

Additive-only: new table, no changes to any existing schema. Reversible
via ``op.drop_table``.

Revision ID: 0011_oss_member_state_rates
Revises:     0010_social_tax_floor
Create Date: 2026-07-11
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0011_oss_member_state_rates"
down_revision: str | None = "0010_social_tax_floor"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLE = "oss_member_state_rates"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "country_code",
            sa.String(3),
            sa.ForeignKey("countries.code"),
            nullable=False,
            comment="ISO 3166-1 alpha-3 — the OSS destination (consumption) member state.",
        ),
        sa.Column(
            "standard_vat_rate_percent",
            sa.Numeric(7, 4),
            nullable=False,
            comment=(
                "Standard VAT rate as a percentage (21.0000 = 21%, not "
                "0.21) — matches RefTaxCode.rate_percent's convention. "
                "Reduced/parking rates are out of scope for this table; "
                "the OSS-Q generator applies the standard rate unless a "
                "company's own OSS TaxCode carries an explicit override "
                "(see services.lodgement.oss_q.generator)."
            ),
        ),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column(
            "effective_to", sa.Date(), nullable=True,
            comment="NULL = still in force.",
        ),
        sa.Column(
            "source_note", sa.String(256), nullable=True,
            comment="Provenance / UNVERIFIED flag for this specific rate row.",
        ),
        sa.UniqueConstraint(
            "country_code", "effective_from",
            name="uq_oss_member_state_rates_country_eff",
        ),
    )


def downgrade() -> None:
    op.drop_table(_TABLE)
