"""PAYG withholding + STSL coefficient tables (Phase 2A).

Two reference tables hold the ATO Schedule 1 (NAT 1004) "statement of
formulas for calculating amounts to be withheld" coefficients:

* ``payg_tax_scales`` — base PAYG withholding coefficients keyed by
  ``(scale_no, period, earnings_band)``.
* ``stsl_coefficients`` — Study and Training Support Loan (HELP/SFSS
  successor) top-up coefficients applied additively on top of PAYG.

Schema shape per the ATO formula

    x  = floor(weekly_earnings + 0.99)            (i.e. + 0.99 cents)
    WH = round(a * x - b)                          (whole dollars)

where ``a`` / ``b`` are the row's ``coef_a`` / ``coef_b``. Each row
covers an inclusive ``earnings_floor`` to non-inclusive
``earnings_ceil`` band (the top band has ``earnings_ceil = NULL``).

A scale is **(scale_no, period)** — even though the canonical formula
is weekly, ATO publishes pre-scaled fortnightly and monthly tables for
direct use. We seed weekly only and convert at calc time; the schema
admits per-period rows for future use.

The ``effective_from`` / ``effective_to`` columns make this an SCD-2
ref table — a row is "live" when ``effective_from <= today < (effective_to
OR +inf)``. Phase 2 lookups use ``effective_date``-bracketing.

NEITHER TABLE IS RLS-GATED. These are public ATO reference data —
they are global to the schema, not per-tenant. Grants follow the
existing "reference data" pattern (e.g. ``tax_codes``).

Revision ID: 0112_payg_tables
Revises: 0111_pay_run_lines_extension
Create Date: 2026-05-22
"""
from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0113_payg_tables"
down_revision: str | None = "0112_pay_run_lines_extension"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PAYG_PERIODS = ("WEEKLY", "FORTNIGHTLY", "MONTHLY")
_APP_ROLE = "saebooks_app"
# Marker used to flag every seeded row until the coefficients have
# been hand-verified against the ATO-published spreadsheet. The
# fy25-26 numbers below are DERIVED from the published marginal-rate
# table + the formula structure in NAT 1004; they have NOT been
# round-tripped against the ATO "Tax tables Excel calculator". See
# tests/services/test_payg.py for the round-trip targets, and
# saebooks/services/payg.py for the formula application.
_SOURCE_DOC_FY25_26 = (
    "NAT 1004 FY25-26 [DERIVED — verify before production]"
)
_SOURCE_DOC_WHM_FY25_26 = (
    "NAT 3539 / Schedule 15 FY25-26 [DERIVED — verify before production]"
)


# --------------------------------------------------------------------- #
# Coefficient tables                                                    #
# --------------------------------------------------------------------- #
#
# Format: (earnings_floor, earnings_ceil, coef_a, coef_b)
#  - earnings_floor / earnings_ceil are WEEKLY DOLLARS
#  - coef_a / coef_b come from WH = a*x - b
#  - earnings_ceil = None for the top band
#
# All values quoted to 6 decimal places to match the column precision.
#
# Derivation (FY25-26 resident, CLAIMS TFT — "Scale 2"):
#   Annual brackets (Stage 3, in force from 1 Jul 2024):
#     0       –  $18,200  : 0%
#     $18,201 –  $45,000  : 16%
#     $45,001 – $135,000  : 30%
#     $135,001– $190,000  : 37%
#     $190,001+           : 45%
#   Medicare levy: 2% above the medicare-shading lower threshold
#   (~$26k for single, full shaded by ~$32k). The ATO coefficients
#   roll this in to bands 2-5; bands 1 (sub-LITO) cover the LITO
#   shade-out separately.
#
# WHAT IS DERIVED HERE: a simplified "main-rates" approximation that
# treats medicare as 2% applied from the second band upward and
# ignores LITO + medicare shading. THIS IS NOT ATO-EXACT.
# The intent is to give a self-consistent table the engine can
# round-trip against in tests, with the precise ATO values dropped in
# by Phase 2B before any client runs payroll for real.
# --------------------------------------------------------------------- #


def _w(x: float | str | Decimal) -> Decimal:
    """Shorthand to Decimal-quantize a coefficient or boundary."""
    return Decimal(str(x))


# Scale 2 — resident, claims tax-free threshold (most common).
# Weekly equivalent boundaries: 18200/52=350.00, 45000/52=865.38,
# 135000/52=2596.15, 190000/52=3653.85.
_SCALE_2_WEEKLY: list[tuple[Decimal, Decimal | None, Decimal, Decimal]] = [
    (_w("0.00"),     _w("350.00"),   _w("0.000000"),  _w("0.000000")),
    (_w("350.00"),   _w("865.38"),   _w("0.180000"),  _w("63.000000")),    # 16% + 2% MC
    (_w("865.38"),   _w("2596.15"),  _w("0.320000"),  _w("184.150000")),   # 30% + 2% MC
    (_w("2596.15"),  _w("3653.85"),  _w("0.390000"),  _w("365.880000")),   # 37% + 2% MC
    (_w("3653.85"),  None,           _w("0.470000"),  _w("658.190000")),   # 45% + 2% MC
]

# Scale 1 — non-resident (no TFT, no medicare levy).
# Non-resident brackets FY25-26:
#   0      – $135,000 : 30%
#   $135k+ – $190,000 : 37%
#   $190k+            : 45%
_SCALE_1_WEEKLY: list[tuple[Decimal, Decimal | None, Decimal, Decimal]] = [
    (_w("0.00"),     _w("2596.15"),  _w("0.300000"),  _w("0.000000")),
    (_w("2596.15"),  _w("3653.85"),  _w("0.370000"),  _w("181.730000")),
    (_w("3653.85"),  None,           _w("0.450000"),  _w("474.040000")),
]

# Scale 3 — resident, NOT claiming TFT (second job).
# No tax-free band; same marginal rates as Scale 2 from $0.
_SCALE_3_WEEKLY: list[tuple[Decimal, Decimal | None, Decimal, Decimal]] = [
    (_w("0.00"),     _w("865.38"),   _w("0.180000"),  _w("0.000000")),
    (_w("865.38"),   _w("2596.15"),  _w("0.320000"),  _w("121.150000")),
    (_w("2596.15"),  _w("3653.85"),  _w("0.390000"),  _w("302.880000")),
    (_w("3653.85"),  None,           _w("0.470000"),  _w("595.190000")),
]

# Scale 4 — no TFN provided.
# 47% flat for residents (45% top marginal + 2% medicare).
# 45% flat for non-residents (handled by a separate row set; we don't
# expose it as a sub-scale in v1 — service layer detects non-resident
# + no-TFN and uses the non-resident-no-TFN value).
_SCALE_4_WEEKLY: list[tuple[Decimal, Decimal | None, Decimal, Decimal]] = [
    (_w("0.00"),  None, _w("0.470000"), _w("0.000000")),
]
# Non-resident no-TFN: 45% flat.
_SCALE_4_NONRES_WEEKLY: list[tuple[Decimal, Decimal | None, Decimal, Decimal]] = [
    (_w("0.00"),  None, _w("0.450000"), _w("0.000000")),
]

# Scale 5 — full medicare exemption (e.g. temporary residents on
# qualifying visas with form NAT 0929 lodged). Same as Scale 2 minus
# the 2% medicare layer.
_SCALE_5_WEEKLY: list[tuple[Decimal, Decimal | None, Decimal, Decimal]] = [
    (_w("0.00"),     _w("350.00"),   _w("0.000000"),  _w("0.000000")),
    (_w("350.00"),   _w("865.38"),   _w("0.160000"),  _w("56.000000")),
    (_w("865.38"),   _w("2596.15"),  _w("0.300000"),  _w("177.150000")),
    (_w("2596.15"),  _w("3653.85"),  _w("0.370000"),  _w("358.880000")),
    (_w("3653.85"),  None,           _w("0.450000"),  _w("651.190000")),
]

# Scale 6 — half medicare exemption. Same as Scale 2 with 1% medicare.
_SCALE_6_WEEKLY: list[tuple[Decimal, Decimal | None, Decimal, Decimal]] = [
    (_w("0.00"),     _w("350.00"),   _w("0.000000"),  _w("0.000000")),
    (_w("350.00"),   _w("865.38"),   _w("0.170000"),  _w("59.500000")),
    (_w("865.38"),   _w("2596.15"),  _w("0.310000"),  _w("180.650000")),
    (_w("2596.15"),  _w("3653.85"),  _w("0.380000"),  _w("362.380000")),
    (_w("3653.85"),  None,           _w("0.460000"),  _w("654.690000")),
]

# Scale 7 — Working Holiday Maker (per NAT 3539 / Schedule 15).
# WHM rates FY25-26 (post-Stage-3):
#   0       – $45,000  : 15%
#   $45,001 – $135,000 : 30%
#   $135,001– $190,000 : 37%
#   $190,001+          : 45%
# WHM has NO medicare component (non-resident for medicare purposes).
_SCALE_7_WEEKLY: list[tuple[Decimal, Decimal | None, Decimal, Decimal]] = [
    (_w("0.00"),     _w("865.38"),   _w("0.150000"),  _w("0.000000")),
    (_w("865.38"),   _w("2596.15"),  _w("0.300000"),  _w("129.810000")),
    (_w("2596.15"),  _w("3653.85"),  _w("0.370000"),  _w("311.540000")),
    (_w("3653.85"),  None,           _w("0.450000"),  _w("603.850000")),
]

_SCALES_FY25_26: dict[int, list[tuple[Decimal, Decimal | None, Decimal, Decimal]]] = {
    1: _SCALE_1_WEEKLY,
    2: _SCALE_2_WEEKLY,
    3: _SCALE_3_WEEKLY,
    4: _SCALE_4_WEEKLY,
    5: _SCALE_5_WEEKLY,
    6: _SCALE_6_WEEKLY,
    7: _SCALE_7_WEEKLY,
    # Non-resident no-TFN gets pseudo-scale 8 internally.
    8: _SCALE_4_NONRES_WEEKLY,
}

# STSL (study/training support loan) coefficients FY25-26.
# Applied additively on top of PAYG. Thresholds & rates per the
# Higher Education Support Act / VETSL repayment income table.
# Repayment rate runs 1% (at $54,435) to 10% (at $158,003+) — bands
# as published. DERIVED weekly conversion; verify before production.
_STSL_WEEKLY: list[tuple[Decimal, Decimal | None, Decimal, Decimal]] = [
    # Repayment income (annual) → weekly band lower bound
    # Bands (FY25-26): 0/1/2/.../9/10 percent of repayment income.
    (_w("0.00"),     _w("1046.83"),  _w("0.000000"),  _w("0.000000")),     # < $54,435
    (_w("1046.83"),  _w("1209.94"),  _w("0.010000"),  _w("0.000000")),     # 1%
    (_w("1209.94"),  _w("1282.50"),  _w("0.020000"),  _w("12.099400")),    # 2%
    (_w("1282.50"),  _w("1359.10"),  _w("0.025000"),  _w("18.512500")),    # 2.5%
    (_w("1359.10"),  _w("1440.43"),  _w("0.030000"),  _w("25.305500")),    # 3%
    (_w("1440.43"),  _w("1525.70"),  _w("0.035000"),  _w("32.507650")),    # 3.5%
    (_w("1525.70"),  _w("1616.71"),  _w("0.040000"),  _w("40.135500")),    # 4%
    (_w("1616.71"),  _w("1713.46"),  _w("0.045000"),  _w("48.218550")),    # 4.5%
    (_w("1713.46"),  _w("1815.91"),  _w("0.050000"),  _w("56.785900")),    # 5%
    (_w("1815.91"),  _w("1924.06"),  _w("0.055000"),  _w("65.865455")),    # 5.5%
    (_w("1924.06"),  _w("2039.61"),  _w("0.060000"),  _w("75.484600")),    # 6%
    (_w("2039.61"),  _w("2161.91"),  _w("0.065000"),  _w("85.682050")),    # 6.5%
    (_w("2161.91"),  _w("2291.62"),  _w("0.070000"),  _w("96.491550")),    # 7%
    (_w("2291.62"),  _w("2429.45"),  _w("0.075000"),  _w("107.949600")),   # 7.5%
    (_w("2429.45"),  _w("2575.45"),  _w("0.080000"),  _w("120.096850")),   # 8%
    (_w("2575.45"),  _w("2729.91"),  _w("0.085000"),  _w("132.974100")),   # 8.5%
    (_w("2729.91"),  _w("2893.41"),  _w("0.090000"),  _w("146.624450")),   # 9%
    (_w("2893.41"),  _w("3038.52"),  _w("0.095000"),  _w("161.091500")),   # 9.5%
    (_w("3038.52"),  None,           _w("0.100000"),  _w("176.283100")),   # 10%
]

# Effective dates: FY25-26 = 1 Jul 2025 to 30 Jun 2026.
from datetime import date as _date_cls

_FY25_26_START = _date_cls(2025, 7, 1)
_FY25_26_END = _date_cls(2026, 6, 30)


# --------------------------------------------------------------------- #
# upgrade                                                               #
# --------------------------------------------------------------------- #


def upgrade() -> None:
    # ENUM for period — re-uses the value tuple from pay_frequency_enum
    # only by coincidence (the column meaning differs), so a separate
    # type avoids accidental cross-coupling.
    payg_period_enum = postgresql.ENUM(
        *PAYG_PERIODS, name="payg_period_enum",
    )
    payg_period_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "payg_tax_scales",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("scale_no", sa.Integer(), nullable=False),
        sa.Column(
            "period",
            postgresql.ENUM(
                *PAYG_PERIODS, name="payg_period_enum", create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("earnings_floor", sa.Numeric(14, 2), nullable=False),
        sa.Column("earnings_ceil", sa.Numeric(14, 2)),
        sa.Column("coef_a", sa.Numeric(8, 6), nullable=False),
        sa.Column("coef_b", sa.Numeric(14, 6), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date()),
        sa.Column("source_doc", sa.String(256), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint(
            "earnings_ceil IS NULL OR earnings_ceil > earnings_floor",
            name="ck_payg_tax_scales_band_ascending",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_payg_tax_scales_dates_ascending",
        ),
        sa.CheckConstraint(
            "scale_no >= 1 AND scale_no <= 8",
            name="ck_payg_tax_scales_scale_range",
        ),
        sa.CheckConstraint(
            "coef_a >= 0",
            name="ck_payg_tax_scales_coef_a_nonneg",
        ),
    )
    op.create_index(
        "ix_payg_tax_scales_lookup",
        "payg_tax_scales",
        ["scale_no", "period", "earnings_floor", "effective_from"],
    )
    op.create_index(
        "ix_payg_tax_scales_effective_range",
        "payg_tax_scales",
        ["effective_from", "effective_to"],
    )

    op.create_table(
        "stsl_coefficients",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "period",
            postgresql.ENUM(
                *PAYG_PERIODS, name="payg_period_enum", create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("earnings_floor", sa.Numeric(14, 2), nullable=False),
        sa.Column("earnings_ceil", sa.Numeric(14, 2)),
        sa.Column("coef_a", sa.Numeric(8, 6), nullable=False),
        sa.Column("coef_b", sa.Numeric(14, 6), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date()),
        sa.Column("source_doc", sa.String(256), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint(
            "earnings_ceil IS NULL OR earnings_ceil > earnings_floor",
            name="ck_stsl_coefficients_band_ascending",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_stsl_coefficients_dates_ascending",
        ),
        sa.CheckConstraint(
            "coef_a >= 0",
            name="ck_stsl_coefficients_coef_a_nonneg",
        ),
    )
    op.create_index(
        "ix_stsl_coefficients_lookup",
        "stsl_coefficients",
        ["period", "earnings_floor", "effective_from"],
    )

    op.execute(f"GRANT SELECT ON payg_tax_scales TO {_APP_ROLE}")
    op.execute(f"GRANT SELECT ON stsl_coefficients TO {_APP_ROLE}")

    # --- Seed FY25-26 PAYG bands -------------------------------------- #
    conn = op.get_bind()
    insert_payg = sa.text(
        "INSERT INTO payg_tax_scales "
        "(scale_no, period, earnings_floor, earnings_ceil, coef_a, coef_b, "
        " effective_from, effective_to, source_doc) "
        "VALUES "
        "(:scale_no, :period, :floor, :ceil, :a, :b, :ef_from, :ef_to, :src)"
    )
    for scale_no, bands in _SCALES_FY25_26.items():
        src = (
            _SOURCE_DOC_WHM_FY25_26 if scale_no == 7 else _SOURCE_DOC_FY25_26
        )
        for floor, ceil, a, b in bands:
            conn.execute(
                insert_payg,
                {
                    "scale_no": scale_no,
                    "period": "WEEKLY",
                    "floor": floor,
                    "ceil": ceil,
                    "a": a,
                    "b": b,
                    "ef_from": _FY25_26_START,
                    "ef_to": _FY25_26_END,
                    "src": src,
                },
            )

    # --- Seed FY25-26 STSL bands -------------------------------------- #
    insert_stsl = sa.text(
        "INSERT INTO stsl_coefficients "
        "(period, earnings_floor, earnings_ceil, coef_a, coef_b, "
        " effective_from, effective_to, source_doc) "
        "VALUES "
        "(:period, :floor, :ceil, :a, :b, :ef_from, :ef_to, :src)"
    )
    for floor, ceil, a, b in _STSL_WEEKLY:
        conn.execute(
            insert_stsl,
            {
                "period": "WEEKLY",
                "floor": floor,
                "ceil": ceil,
                "a": a,
                "b": b,
                "ef_from": _FY25_26_START,
                "ef_to": _FY25_26_END,
                "src": "STSL FY25-26 [DERIVED — verify before production]",
            },
        )


def downgrade() -> None:
    op.drop_index("ix_stsl_coefficients_lookup", table_name="stsl_coefficients")
    op.drop_table("stsl_coefficients")
    op.drop_index("ix_payg_tax_scales_effective_range", table_name="payg_tax_scales")
    op.drop_index("ix_payg_tax_scales_lookup", table_name="payg_tax_scales")
    op.drop_table("payg_tax_scales")
    postgresql.ENUM(*PAYG_PERIODS, name="payg_period_enum").drop(
        op.get_bind(), checkfirst=True
    )
