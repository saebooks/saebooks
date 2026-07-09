"""Initial reference DB schema.

Creates every jurisdiction reference table in one shot. After this
migration runs, the DB is empty rate-wise — seed data is loaded by
``python -m saebooks.cli reference-load``.

Revision ID: 0001_initial_reference_schema
Revises:
Create Date: 2026-05-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_initial_reference_schema"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # ---- jurisdictions (the registry every other table FKs into) ----
    op.create_table(
        "jurisdictions",
        sa.Column("code", sa.String(3), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("currency_default", sa.String(3), nullable=False),
        sa.Column("regulator_name", sa.String(128)),
        sa.Column("regulator_protocol", sa.String(64)),
        sa.Column("decimal_places", sa.Integer, nullable=False, server_default="2"),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
    )

    op.create_table(
        "currencies",
        sa.Column("code", sa.String(3), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("decimal_places", sa.Integer, nullable=False, server_default="2"),
        sa.Column("symbol", sa.String(8)),
    )

    op.create_table(
        "countries",
        sa.Column("code", sa.String(3), primary_key=True),
        sa.Column("code_alpha2", sa.String(2), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("currency_default", sa.String(3)),
        sa.Column("in_eu", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("in_eea", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("in_oss", sa.Boolean, nullable=False, server_default=sa.false()),
    )

    direction_enum = sa.Enum("sale", "purchase", "both", name="ref_tax_direction")

    op.create_table(
        "tax_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("jurisdiction", sa.String(3), sa.ForeignKey("jurisdictions.code"), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("rate_percent", sa.Numeric(7, 4), nullable=False, server_default="0"),
        sa.Column("direction", direction_enum, nullable=False),
        sa.Column("is_inclusive", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("reverse_charge", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("gl_account_hint", sa.String(64)),
        sa.Column("effective_from", sa.Date, nullable=False),
        sa.Column("effective_to", sa.Date),
        sa.Column("report_box_keys", postgresql.ARRAY(sa.String)),
        sa.UniqueConstraint(
            "jurisdiction", "code", "effective_from",
            name="uq_ref_tax_codes_jur_code_eff",
        ),
    )

    op.create_table(
        "tax_return_box_definitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("jurisdiction", sa.String(3), sa.ForeignKey("jurisdictions.code"), nullable=False),
        sa.Column("return_type", sa.String(32), nullable=False),
        sa.Column("box_code", sa.String(32), nullable=False),
        sa.Column("box_label", sa.String(256), nullable=False),
        sa.Column("aggregation", sa.String(64), nullable=False),
        sa.Column("feeder_tax_codes", postgresql.ARRAY(sa.String)),
        sa.Column("display_order", sa.Integer, nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "jurisdiction", "return_type", "box_code",
            name="uq_box_def_jur_form_box",
        ),
    )

    op.create_table(
        "tax_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("jurisdiction", sa.String(3), sa.ForeignKey("jurisdictions.code"), nullable=False),
        sa.Column("rule_type", sa.String(64), nullable=False),
        sa.Column("applies_to", sa.String(32), nullable=False),
        sa.Column("condition", postgresql.JSONB, nullable=False),
        sa.Column("result", postgresql.JSONB, nullable=False),
        sa.Column("priority", sa.Integer, nullable=False, server_default="100"),
        sa.Column("effective_from", sa.Date, nullable=False),
        sa.Column("effective_to", sa.Date),
    )

    op.create_table(
        "chart_template",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("jurisdiction", sa.String(3), sa.ForeignKey("jurisdictions.code"), nullable=False),
        sa.Column("account_code", sa.String(32), nullable=False),
        sa.Column("account_name", sa.String(128), nullable=False),
        sa.Column("account_type", sa.String(32), nullable=False),
        sa.Column("default_tax_code", sa.String(32)),
        sa.Column("display_order", sa.Integer, nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "jurisdiction", "account_code",
            name="uq_chart_template_jur_code",
        ),
    )

    op.create_table(
        "fiscal_year_definitions",
        sa.Column("jurisdiction", sa.String(3), sa.ForeignKey("jurisdictions.code"), primary_key=True),
        sa.Column("fy_start_month", sa.Integer, nullable=False),
        sa.Column("fy_start_day", sa.Integer, nullable=False, server_default="1"),
        sa.Column("quarter_anchors", postgresql.ARRAY(sa.Integer), nullable=False),
    )

    op.create_table(
        "income_tax_brackets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("jurisdiction", sa.String(3), sa.ForeignKey("jurisdictions.code"), nullable=False),
        sa.Column("fy_year", sa.Integer, nullable=False),
        sa.Column("taxpayer_type", sa.String(32), nullable=False),
        sa.Column("lower_bound", sa.Numeric(14, 2), nullable=False),
        sa.Column("upper_bound", sa.Numeric(14, 2)),
        sa.Column("rate", sa.Numeric(7, 4), nullable=False),
        sa.Column("base_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
    )

    op.create_table(
        "payg_withholding_scales",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("jurisdiction", sa.String(3), sa.ForeignKey("jurisdictions.code"), nullable=False),
        sa.Column("fy_year", sa.Integer, nullable=False),
        sa.Column("scale_number", sa.Integer, nullable=False),
        sa.Column("weekly_earnings_lower", sa.Numeric(12, 2), nullable=False),
        sa.Column("weekly_earnings_upper", sa.Numeric(12, 2)),
        sa.Column("a_coefficient", sa.Numeric(10, 6), nullable=False),
        sa.Column("b_subtractor", sa.Numeric(12, 2), nullable=False),
    )

    op.create_table(
        "super_guarantee_rates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("fy_year", sa.Integer, nullable=False, unique=True),
        sa.Column("rate", sa.Numeric(7, 4), nullable=False),
    )

    op.create_table(
        "super_contribution_caps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("fy_year", sa.Integer, nullable=False),
        sa.Column("cap_type", sa.String(32), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
    )

    op.create_table(
        "tax_offsets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("jurisdiction", sa.String(3), sa.ForeignKey("jurisdictions.code"), nullable=False),
        sa.Column("fy_year", sa.Integer, nullable=False),
        sa.Column("offset_code", sa.String(32), nullable=False),
        sa.Column("max_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("lower_threshold", sa.Numeric(14, 2)),
        sa.Column("upper_threshold", sa.Numeric(14, 2)),
        sa.Column("taper_rate", sa.Numeric(7, 4)),
    )

    op.create_table(
        "medicare_levy",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("fy_year", sa.Integer, nullable=False),
        sa.Column("taxpayer_type", sa.String(32), nullable=False),
        sa.Column("threshold_no_levy", sa.Numeric(14, 2), nullable=False),
        sa.Column("threshold_full_levy", sa.Numeric(14, 2), nullable=False),
        sa.Column("rate", sa.Numeric(7, 4), nullable=False),
        sa.Column("surcharge_brackets", postgresql.JSONB),
    )

    op.create_table(
        "fbt_rates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("fy_year", sa.Integer, nullable=False, unique=True),
        sa.Column("fbt_rate", sa.Numeric(7, 4), nullable=False),
        sa.Column("type1_gross_up", sa.Numeric(7, 4), nullable=False),
        sa.Column("type2_gross_up", sa.Numeric(7, 4), nullable=False),
        sa.Column("statutory_interest_rate", sa.Numeric(7, 4), nullable=False),
        sa.Column("car_parking_threshold", sa.Numeric(14, 2), nullable=False),
    )

    op.create_table(
        "ato_interest_rates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("quarter_start", sa.Date, nullable=False),
        sa.Column("quarter_end", sa.Date, nullable=False),
        sa.Column("gic_rate", sa.Numeric(7, 4), nullable=False),
        sa.Column("sic_rate", sa.Numeric(7, 4), nullable=False),
        sa.Column("lpi_rate", sa.Numeric(7, 4), nullable=False),
    )

    op.create_table(
        "fuel_tax_credit_rates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("period_start", sa.Date, nullable=False),
        sa.Column("period_end", sa.Date, nullable=False),
        sa.Column("fuel_type", sa.String(64), nullable=False),
        sa.Column("vehicle_type", sa.String(64), nullable=False),
        sa.Column("rate_cents_per_litre", sa.Numeric(8, 3), nullable=False),
    )

    op.create_table(
        "gst_registration_threshold",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("jurisdiction", sa.String(3), sa.ForeignKey("jurisdictions.code"), nullable=False),
        sa.Column("fy_year", sa.Integer, nullable=False),
        sa.Column("threshold", sa.Numeric(14, 2), nullable=False),
        sa.Column("applies_to", sa.String(32), nullable=False),
    )

    op.create_table(
        "payroll_tax_rates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("jurisdiction", sa.String(3), sa.ForeignKey("jurisdictions.code"), nullable=False),
        sa.Column("state", sa.String(8), nullable=False),
        sa.Column("fy_year", sa.Integer, nullable=False),
        sa.Column("threshold", sa.Numeric(14, 2), nullable=False),
        sa.Column("rate", sa.Numeric(7, 4), nullable=False),
        sa.Column("deduction_formula", postgresql.JSONB),
    )

    op.create_table(
        "fx_rate_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("snapshot_date", sa.Date, nullable=False),
        sa.Column("base_currency", sa.String(3), sa.ForeignKey("currencies.code"), nullable=False),
        sa.Column("quote_currency", sa.String(3), sa.ForeignKey("currencies.code"), nullable=False),
        sa.Column("rate", sa.Numeric(18, 8), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.UniqueConstraint(
            "snapshot_date", "base_currency", "quote_currency", "source",
            name="uq_ref_fx_date_pair_source",
        ),
    )

    op.create_table(
        "tax_id_validation_patterns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("jurisdiction", sa.String(3), sa.ForeignKey("jurisdictions.code"), nullable=False),
        sa.Column("pattern_type", sa.String(32), nullable=False),
        sa.Column("regex", sa.String, nullable=False),
        sa.Column("checksum_algorithm", sa.String(64)),
    )

    op.create_table(
        "holiday_calendars",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("jurisdiction", sa.String(3), sa.ForeignKey("jurisdictions.code"), nullable=False),
        sa.Column("state", sa.String(8)),
        sa.Column("holiday_date", sa.Date, nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("is_business_day_substituted", sa.Boolean, nullable=False, server_default=sa.false()),
    )

    op.create_table(
        "industry_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("jurisdiction", sa.String(3), sa.ForeignKey("jurisdictions.code"), nullable=False),
        sa.Column("code_system", sa.String(16), nullable=False),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("description", sa.String, nullable=False),
        sa.Column("parent_code", sa.String(16)),
    )

    op.create_table(
        "bsb_directory",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("bsb", sa.String(6), nullable=False, unique=True),
        sa.Column("bank_name", sa.String(128), nullable=False),
        sa.Column("branch_name", sa.String(128)),
        sa.Column("address", sa.String(256)),
        sa.Column("suburb", sa.String(64)),
        sa.Column("state", sa.String(8)),
        sa.Column("postcode", sa.String(8)),
        sa.Column("payment_flags", postgresql.ARRAY(sa.String)),
    )

    op.create_table(
        "depreciation_effective_lives",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("jurisdiction", sa.String(3), sa.ForeignKey("jurisdictions.code"), nullable=False),
        sa.Column("asset_class", sa.String(128), nullable=False),
        sa.Column("asset_subclass", sa.String(128)),
        sa.Column("effective_life_years", sa.Numeric(6, 2), nullable=False),
        sa.Column("source_ruling", sa.String(64)),
    )

    op.create_table(
        "stamp_duty_rates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("jurisdiction", sa.String(3), sa.ForeignKey("jurisdictions.code"), nullable=False),
        sa.Column("state", sa.String(8), nullable=False),
        sa.Column("transaction_type", sa.String(64), nullable=False),
        sa.Column("lower_bound", sa.Numeric(14, 2), nullable=False),
        sa.Column("upper_bound", sa.Numeric(14, 2)),
        sa.Column("rate", sa.Numeric(7, 4), nullable=False),
        sa.Column("base_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
    )

    op.create_table(
        "schema_meta",
        sa.Column("id", sa.Integer, primary_key=True, server_default="1"),
        sa.Column("version_tag", sa.String(64), nullable=False),
        sa.Column(
            "loaded_at", sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"), nullable=False,
        ),
    )


def downgrade() -> None:
    # Drop in reverse dependency order (FKs back to jurisdictions/currencies).
    for tbl in [
        "schema_meta",
        "stamp_duty_rates",
        "depreciation_effective_lives",
        "bsb_directory",
        "industry_codes",
        "holiday_calendars",
        "tax_id_validation_patterns",
        "fx_rate_snapshots",
        "payroll_tax_rates",
        "gst_registration_threshold",
        "fuel_tax_credit_rates",
        "ato_interest_rates",
        "fbt_rates",
        "medicare_levy",
        "tax_offsets",
        "super_contribution_caps",
        "super_guarantee_rates",
        "payg_withholding_scales",
        "income_tax_brackets",
        "fiscal_year_definitions",
        "chart_template",
        "tax_rules",
        "tax_return_box_definitions",
        "tax_codes",
        "countries",
        "currencies",
        "jurisdictions",
    ]:
        op.drop_table(tbl)
    sa.Enum(name="ref_tax_direction").drop(op.get_bind(), checkfirst=True)
