"""Replace placeholder PAYG + STSL coefficients with verified ATO FY25-26 values.

Three independent problems are corrected by this migration:

Problem 1 — Scale numbering inverted
  The original seeding in 0113_payg_tables.py used internal scale numbers
  that do not match ATO publication conventions:
    DB 0113 scale 1 = foreign resident (ATO scale 3)
    DB 0113 scale 2 = resident TFT claimed (ATO scale 2) — correct
    DB 0113 scale 3 = resident no TFT (ATO scale 1)
  This migration renumbers the DB to match ATO scale numbering, so that
  service-layer code written against the ATO spec looks up the right bands.
  Code callers (see below) MUST be updated before this migration is applied.

Problem 2 — Coefficient values wrong
  Every scale except the foreign-resident one had material discrepancies:
  max $108/wk underwithholding on scale 1 (resident no TFT) at $5,000/wk.
  Scale 2 (resident TFT, most common): $7–8/wk underwithholding above $700/wk.
  Root cause: original bands were simplified approximations (missing LITO
  shade-out detail and Medicare levy shading breakpoints).

Problem 3 — stsl_coefficients superseded
  The old percentage-of-income system (19 bands, 1%–10%) was replaced by
  ATO effective 24 September 2025 with a new 4-band marginal formula
  under Schedule 8 (NAT 3539). The new system has two variants:
    - TFT claimed / foreign resident variant
    - No TFT variant
  The current stsl_coefficients rows are entirely from the old system.

Source documents
  payg_tax_scales: ATO Schedule 1 NAT 1004, FY25-26 (confirmed current
    as at 2026-05-23). URL:
    https://www.ato.gov.au/tax-rates-and-codes/payg-withholding-schedule-1
    -statement-of-formulas-for-calculating-amounts-to-be-withheld
  stsl_coefficients: ATO Schedule 8 NAT 3539, effective 24 Sep 2025.
    URL: https://www.ato.gov.au/tax-rates-and-codes/schedule-8-statement-
    of-formulas-for-calculating-study-and-training-support-loans-components
  WHM scale 7: Schedule 15 NAT 75331. No b-offset coefficients published
    by ATO (Schedule 15 uses y=a*x cumulative, not per-period a*x-b).
    The b values in scale 7 are a derived per-period approximation —
    see verification report for the design decision outstanding.

Verification report
  See an internal verification note (2026-05-23).
  All payg_tax_scales a/b values are transcribed verbatim from the
  ATO NAT 1004 HTML coefficient table (fetched 2026-05-23).
  stsl_coefficients a/b values: ATO-verified 2026-05-23 against NAT 3539
  Schedule 8 HTML. URL: https://www.ato.gov.au/tax-rates-and-codes/schedule-8-
  statement-of-formulas-for-calculating-study-and-training-support-loans-
  components — prior agent estimates corrected (band3 a 0.30→0.17, band4
  a 0.50→0.10, band2 b 193.1485→193.2692). See inline comments in
  _STSL_TFT_WEEKLY for detail.

down_revision note
  This migration targets 0119_account_kind as down_revision, which is
  the actual pushed HEAD as of 2026-05-23. Despite appearing as
  "untracked" in an earlier working-tree snapshot, 0119_account_kind.py
  is committed in the c764ff7 HEAD commit (feat: bank-accounts account_kind).
  The number 0120 is chosen to be sequentially next after 0119.

scale_no callers needing follow-up BEFORE applying this migration
  The service layer uses internal scale constants that currently map to
  the OLD (pre-ATO) numbering from 0113_payg_tables. After this migration
  the DB will use ATO numbering. The following must be updated together:

  saebooks/services/payg.py:134  _SCALE_NONRES = 1
    Currently 1 (foreign resident). After renumber: should be 3.
    Impact: non-resident payees will look up ATO Scale 1 (resident no TFT)
    instead of Scale 3 (foreign resident). WRONG WITHHOLDING.

  saebooks/services/payg.py:135  _SCALE_RES_TFT = 2
    Currently 2. After renumber: still 2 (ATO Scale 2 = TFT claimed).
    No change needed — the numbering happens to be correct here.

  saebooks/services/payg.py:136  _SCALE_RES_NO_TFT = 3
    Currently 3 (resident no TFT). After renumber: should be 1.
    Impact: resident no-TFT payees will look up ATO Scale 3 (foreign
    resident) instead of Scale 1. WRONG WITHHOLDING.

  saebooks/services/payg.py:137  _SCALE_NO_TFN_RES = 4
    Currently 4. ATO Scale 4 = no TFN flat. No change needed.

  saebooks/services/payg.py:138  _SCALE_FULL_MED_EXEMPT = 5
    Currently 5. ATO Scale 5 = full Medicare exempt. No change needed.

  saebooks/services/payg.py:139  _SCALE_HALF_MED_EXEMPT = 6
    Currently 6. ATO Scale 6 = half Medicare exempt. No change needed.

  saebooks/services/payg.py:140  _SCALE_WHM = 7
    Currently 7 (WHM). No ATO scale 7 equivalent — WHM uses Schedule 15.
    The internal mapping is unchanged; the DB still stores it as scale 7.

  saebooks/services/payg.py:141  _SCALE_NO_TFN_NONRES = 8
    Currently 8 (non-resident no TFN flat 45%). No ATO equivalent —
    internal pseudo-scale. No change needed.

  saebooks/models/payg.py:58   CheckConstraint scale_no >= 1 AND scale_no <= 8
    Still valid (scales 1–8). No change needed.

  saebooks/services/payg.py:42-55  Scale resolution docstring table
    Documents scale_no 1=non-resident, 3=resident-no-TFT. After fix:
    should read 3=non-resident, 1=resident-no-TFT. Update for clarity.

  DO NOT APPLY this migration until _SCALE_NONRES and _SCALE_RES_NO_TFT
  constants in payg.py are swapped to match ATO numbering.

Migration NOT APPLIED — for Richard to review before alembic upgrade head.

Revision ID: 0120_payg_tax_scales_fy25_26
Revises: 0119_account_kind
Create Date: 2026-05-23
"""
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

import sqlalchemy as sa

from alembic import op

revision: str = "0120_payg_tax_scales_fy25_26"
down_revision: str | None = "0119_account_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --------------------------------------------------------------------- #
# Source doc strings                                                    #
# --------------------------------------------------------------------- #

_SRC_NAT1004 = (
    "NAT 1004 FY25-26 ATO Schedule 1 — verbatim from ATO HTML table "
    "fetched 2026-05-23; verification: internal verification note, 2026-05-23"
)
_SRC_NAT1004_WHM = (
    "NAT 75331 Schedule 15 FY25-26 — a values match ATO; b values are "
    "derived per-period approximation (ATO publishes y=a*x cumulative only). "
    "Design decision outstanding: see verification report."
)
_SRC_NAT3539 = (
    "NAT 3539 Schedule 8 effective 2025-09-24 — ATO Schedule 8 verbatim, "
    "fetched 2026-05-23. URL: https://www.ato.gov.au/tax-rates-and-codes/"
    "schedule-8-statement-of-formulas-for-calculating-study-and-training-"
    "support-loans-components"
)

_FY25_26_START = date(2025, 7, 1)
_FY25_26_END = date(2026, 6, 30)
_STSL_START = date(2025, 9, 24)   # new marginal formula effective date


# --------------------------------------------------------------------- #
# PAYG tax scale bands — verbatim from ATO NAT 1004 FY25-26            #
# Format: (earnings_floor, earnings_ceil_or_None, coef_a, coef_b)      #
# All values transcribed from the ATO HTML table, fetched 2026-05-23.  #
# --------------------------------------------------------------------- #

# ATO Scale 1 — Resident, NO tax-free threshold claimed (second job).
# 7 bands — includes LITO shade-out detail (bands 2–4) and Medicare
# levy shading (bands 4–5) at low income.
_SCALE_1_WEEKLY = [
    # floor,    ceil,      a,          b
    ("0.00",    "150.00",  "0.160000", "0.160000"),
    ("150.00",  "371.00",  "0.211700", "7.755000"),
    ("371.00",  "515.00",  "0.189000", "-0.670200"),
    ("515.00",  "932.00",  "0.322700", "68.236700"),
    ("932.00",  "2246.00", "0.320000", "65.720200"),
    ("2246.00", "3303.00", "0.390000", "222.951000"),
    ("3303.00", None,      "0.470000", "487.258700"),
]

# ATO Scale 2 — Resident, tax-free threshold claimed (main job).
# 9 bands — includes nil band, LITO shade-out detail, and Medicare levy
# shading breakpoints.
_SCALE_2_WEEKLY = [
    # floor,    ceil,      a,          b
    ("0.00",    "361.00",  "0.000000", "0.000000"),   # nil (below LITO+TFT)
    ("361.00",  "500.00",  "0.160000", "57.846200"),
    ("500.00",  "625.00",  "0.260000", "107.846200"),
    ("625.00",  "721.00",  "0.180000", "57.846200"),
    ("721.00",  "865.00",  "0.189000", "64.336500"),
    ("865.00",  "1282.00", "0.322700", "180.038500"),
    ("1282.00", "2596.00", "0.320000", "176.576900"),
    ("2596.00", "3653.00", "0.390000", "358.307700"),
    ("3653.00", None,      "0.470000", "650.615400"),
]

# ATO Scale 3 — Foreign residents (no tax-free threshold, no Medicare).
# 3 bands. Coefficient values effectively match existing DB scale 1
# (within sub-cent rounding) — the b values are updated to ATO-exact.
_SCALE_3_WEEKLY = [
    # floor,    ceil,      a,          b
    ("0.00",    "2596.15", "0.300000", "0.300000"),
    ("2596.15", "3653.85", "0.370000", "181.730800"),
    ("3653.85", None,      "0.450000", "474.038500"),
]

# ATO Scale 4 — No TFN provided.
# Single flat-rate band: 47% resident, 45% non-resident.
# The non-resident variant is stored as internal pseudo-scale 8.
_SCALE_4_RES_WEEKLY = [
    ("0.00", None, "0.470000", "0.000000"),
]
_SCALE_8_NONRES_NO_TFN_WEEKLY = [
    ("0.00", None, "0.450000", "0.000000"),
]

# ATO Scale 5 — Resident, full Medicare levy exemption.
# 7 bands. Mirrors Scale 2 structure without Medicare. Includes the
# corrected a=0.1690 in band 3 (DB had 0.1600, a/b combined to produce
# similar results by coincidence — now corrected to ATO exact).
_SCALE_5_WEEKLY = [
    # floor,    ceil,      a,          b
    ("0.00",    "361.00",  "0.000000", "0.000000"),
    ("361.00",  "721.00",  "0.160000", "57.846200"),
    ("721.00",  "865.00",  "0.169000", "64.336500"),
    ("865.00",  "1282.00", "0.302700", "180.038500"),
    ("1282.00", "2596.00", "0.300000", "176.576900"),
    ("2596.00", "3653.00", "0.370000", "358.307700"),
    ("3653.00", None,      "0.450000", "650.615400"),
]

# ATO Scale 6 — Resident, half Medicare levy exemption.
# 9 bands — Medicare shading adds two extra breakpoints at $843 and
# $1,053 compared to Scale 5.
_SCALE_6_WEEKLY = [
    # floor,    ceil,      a,          b
    ("0.00",    "361.00",  "0.000000", "0.000000"),
    ("361.00",  "721.00",  "0.160000", "57.846200"),
    ("721.00",  "843.00",  "0.169000", "64.336500"),
    ("843.00",  "865.00",  "0.219000", "106.496200"),
    ("865.00",  "1053.00", "0.352700", "222.198100"),
    ("1053.00", "1282.00", "0.312700", "180.038500"),
    ("1282.00", "2596.00", "0.310000", "176.576900"),
    ("2596.00", "3653.00", "0.380000", "358.307700"),
    ("3653.00", None,      "0.460000", "650.615400"),
]

# Internal pseudo-scale 7 — Working Holiday Maker (Schedule 15).
# The ATO publishes Schedule 15 as a cumulative-income annual formula
# (y = a*x, no b term). These per-period a/b values are a derived
# approximation matching the WHM rates at the published annual thresholds.
# The a values match ATO Schedule 15 Table A rates exactly; the b values
# are placeholder offsets that produce correct withholding at threshold
# crossings but have not been round-tripped against ATO sample data.
# Design decision outstanding: see verification report § "DB Scale 7".
_SCALE_7_WHM_WEEKLY = [
    # floor,    ceil,      a,          b
    ("0.00",    "865.38",  "0.150000", "0.000000"),
    ("865.38",  "2596.15", "0.300000", "129.810000"),
    ("2596.15", "3653.85", "0.370000", "311.540000"),
    ("3653.85", None,      "0.450000", "603.850000"),
]

# All scales keyed by ATO scale number (1–6 are NAT 1004 official;
# 7 and 8 are internal pseudo-scales).
_SCALES_FY25_26: dict[int, list] = {
    1: _SCALE_1_WEEKLY,
    2: _SCALE_2_WEEKLY,
    3: _SCALE_3_WEEKLY,
    4: _SCALE_4_RES_WEEKLY,
    5: _SCALE_5_WEEKLY,
    6: _SCALE_6_WEEKLY,
    7: _SCALE_7_WHM_WEEKLY,
    8: _SCALE_8_NONRES_NO_TFN_WEEKLY,
}

_SCALE_SOURCE: dict[int, str] = {
    1: _SRC_NAT1004,
    2: _SRC_NAT1004,
    3: _SRC_NAT1004,
    4: _SRC_NAT1004,
    5: _SRC_NAT1004,
    6: _SRC_NAT1004,
    7: _SRC_NAT1004_WHM,
    8: _SRC_NAT1004,
}


# --------------------------------------------------------------------- #
# STSL bands — Schedule 8 (NAT 3539) effective 24 September 2025       #
#                                                                       #
# The new system has two variants keyed on whether the employee has     #
# claimed the tax-free threshold (TFT) or is a foreign resident.       #
#                                                                       #
# IMPORTANT: The stsl_coefficients table has no tft_variant column.    #
# The existing service layer (_lookup_stsl_band) queries by period +   #
# earnings x + effective_date only. This migration seeds ONLY the TFT  #
# claimed / foreign resident variant (lower thresholds = higher         #
# withholding = safer). The no-TFT variant requires a schema change     #
# (add tft_variant column or separate table) — flagged for Phase 2C.  #
#                                                                       #
# Breakpoints (weekly equivalents from ATO NAT 3539):                  #
#   TFT/FR: $1,288 / $2,403 / $3,447                                  #
#   No TFT: $938 / $2,053 / $2,597 (NOT SEEDED — schema gap)          #
#                                                                       #
# a/b values: ATO-verified 2026-05-23. Verbatim from NAT 3539 Schedule 8. #
# Source: https://www.ato.gov.au/tax-rates-and-codes/schedule-8-statement- #
# of-formulas-for-calculating-study-and-training-support-loans-components  #
# Prior agent derivation (a=0.15/0.30/0.50) was WRONG — ATO uses          #
# a=0.15/0.17/0.10. Band 4 drops to a flat 10% HELP repayment cap.        #
# Verification cross-check: ATO Example 1 ($2,608.36 TFT): x=2608.99,     #
# y = 0.17*2608.99 - 241.3462 = 202.18 -> $202. Confirmed.                #
# --------------------------------------------------------------------- #

# TFT-claimed / foreign-resident variant (4 bands). ATO-verified 2026-05-23.
# Band 1: nil (< $1,288/wk, below repayment threshold). a=0.00, b=0.0000.
# Band 2: $1,288-$2,403/wk. a=0.15, b=193.2692. ATO NAT 3539 verbatim.
# Band 3: $2,403-$3,447/wk. a=0.17, b=241.3462. ATO NAT 3539 verbatim.
# Band 4: $3,447+/wk. a=0.10, b=0.0000. Flat 10% HELP repayment cap.
#   NOTE: Band 4 drops from 17% to 10% -- this is NOT a rising marginal
#   rate system. The ATO caps HELP repayment at 10% of weekly earnings
#   above $3,447/wk (roughly $179k/yr). Confirmed via ATO Example 1.
_STSL_TFT_WEEKLY = [
    # floor,    ceil,      a,          b
    # Verbatim from ATO NAT 3539 Schedule 8 (effective 2025-09-24),
    # ATO-verified 2026-05-23 against:
    # https://www.ato.gov.au/tax-rates-and-codes/schedule-8-statement-of-formulas-for-calculating-study-and-training-support-loans-components
    # Prior agent estimated a=0.15/0.30/0.50 for bands 2-4; ATO actual is 0.15/0.17/0.10.
    # Band 4 drops to 0.10 (flat HELP repayment cap), NOT a rising marginal step.
    ("0.00",    "1288.00", "0.000000", "0.000000"),    # nil band (below repayment threshold)
    ("1288.00", "2403.00", "0.150000", "193.269200"),  # ATO-verified 2026-05-23
    ("2403.00", "3447.00", "0.170000", "241.346200"),  # ATO-verified 2026-05-23
    ("3447.00", None,      "0.100000", "0.000000"),    # ATO-verified 2026-05-23
]


# --------------------------------------------------------------------- #
# Placeholder rows for downgrade (symmetric with 0113_payg_tables)     #
# These are the original placeholder rows that will be re-inserted on  #
# downgrade. Richard has stated rollback is unlikely; these are kept   #
# for schema symmetry only.                                             #
# --------------------------------------------------------------------- #

_PLACEHOLDER_SRC = "NAT 1004 FY25-26 [DERIVED — verify before production]"
_PLACEHOLDER_WHM_SRC = "NAT 3539 / Schedule 15 FY25-26 [DERIVED — verify before production]"
_PLACEHOLDER_STSL_SRC = "STSL FY25-26 [DERIVED — verify before production]"

_PLACEHOLDER_SCALES: dict[int, list] = {
    1: [  # old DB scale 1 = foreign resident
        ("0.00",    "2596.15", "0.300000", "0.000000"),
        ("2596.15", "3653.85", "0.370000", "181.730000"),
        ("3653.85", None,      "0.450000", "474.040000"),
    ],
    2: [  # old DB scale 2 = resident TFT
        ("0.00",    "350.00",  "0.000000", "0.000000"),
        ("350.00",  "865.38",  "0.180000", "63.000000"),
        ("865.38",  "2596.15", "0.320000", "184.150000"),
        ("2596.15", "3653.85", "0.390000", "365.880000"),
        ("3653.85", None,      "0.470000", "658.190000"),
    ],
    3: [  # old DB scale 3 = resident no TFT
        ("0.00",    "865.38",  "0.180000", "0.000000"),
        ("865.38",  "2596.15", "0.320000", "121.150000"),
        ("2596.15", "3653.85", "0.390000", "302.880000"),
        ("3653.85", None,      "0.470000", "595.190000"),
    ],
    4: [("0.00", None, "0.470000", "0.000000")],
    5: [
        ("0.00",    "350.00",  "0.000000", "0.000000"),
        ("350.00",  "865.38",  "0.160000", "56.000000"),
        ("865.38",  "2596.15", "0.300000", "177.150000"),
        ("2596.15", "3653.85", "0.370000", "358.880000"),
        ("3653.85", None,      "0.450000", "651.190000"),
    ],
    6: [
        ("0.00",    "350.00",  "0.000000", "0.000000"),
        ("350.00",  "865.38",  "0.170000", "59.500000"),
        ("865.38",  "2596.15", "0.310000", "180.650000"),
        ("2596.15", "3653.85", "0.380000", "362.380000"),
        ("3653.85", None,      "0.460000", "654.690000"),
    ],
    7: [
        ("0.00",    "865.38",  "0.150000", "0.000000"),
        ("865.38",  "2596.15", "0.300000", "129.810000"),
        ("2596.15", "3653.85", "0.370000", "311.540000"),
        ("3653.85", None,      "0.450000", "603.850000"),
    ],
    8: [("0.00", None, "0.450000", "0.000000")],
}
_PLACEHOLDER_STSL: list = [
    ("0.00",     "1046.83",  "0.000000", "0.000000"),
    ("1046.83",  "1209.94",  "0.010000", "0.000000"),
    ("1209.94",  "1282.50",  "0.020000", "12.099400"),
    ("1282.50",  "1359.10",  "0.025000", "18.512500"),
    ("1359.10",  "1440.43",  "0.030000", "25.305500"),
    ("1440.43",  "1525.70",  "0.035000", "32.507650"),
    ("1525.70",  "1616.71",  "0.040000", "40.135500"),
    ("1616.71",  "1713.46",  "0.045000", "48.218550"),
    ("1713.46",  "1815.91",  "0.050000", "56.785900"),
    ("1815.91",  "1924.06",  "0.055000", "65.865455"),
    ("1924.06",  "2039.61",  "0.060000", "75.484600"),
    ("2039.61",  "2161.91",  "0.065000", "85.682050"),
    ("2161.91",  "2291.62",  "0.070000", "96.491550"),
    ("2291.62",  "2429.45",  "0.075000", "107.949600"),
    ("2429.45",  "2575.45",  "0.080000", "120.096850"),
    ("2575.45",  "2729.91",  "0.085000", "132.974100"),
    ("2729.91",  "2893.41",  "0.090000", "146.624450"),
    ("2893.41",  "3038.52",  "0.095000", "161.091500"),
    ("3038.52",  None,       "0.100000", "176.283100"),
]


# --------------------------------------------------------------------- #
# upgrade                                                               #
# --------------------------------------------------------------------- #


def upgrade() -> None:
    conn = op.get_bind()

    # --- 1. Delete all existing rows ----------------------------------- #
    conn.execute(sa.text("DELETE FROM payg_tax_scales"))
    conn.execute(sa.text("DELETE FROM stsl_coefficients"))

    # --- 2. Insert correct ATO Scale 1–8 bands ------------------------ #
    insert_payg = sa.text(
        "INSERT INTO payg_tax_scales "
        "(scale_no, period, earnings_floor, earnings_ceil, coef_a, coef_b, "
        " effective_from, effective_to, source_doc) "
        "VALUES "
        "(:scale_no, 'WEEKLY', :floor, :ceil, :a, :b, :ef_from, :ef_to, :src)"
    )
    for scale_no, bands in _SCALES_FY25_26.items():
        src = _SCALE_SOURCE[scale_no]
        for row in bands:
            floor, ceil, a, b = row
            conn.execute(
                insert_payg,
                {
                    "scale_no": scale_no,
                    "floor": floor,
                    "ceil": ceil,
                    "a": a,
                    "b": b,
                    "ef_from": _FY25_26_START,
                    "ef_to": _FY25_26_END,
                    "src": src,
                },
            )

    # --- 3. Insert new Schedule 8 STSL bands -------------------------- #
    # NOTE: Only the TFT-claimed/foreign-resident variant is seeded.
    # The no-TFT variant requires a schema change (tft_variant column).
    # See migration docstring for details.
    insert_stsl = sa.text(
        "INSERT INTO stsl_coefficients "
        "(period, earnings_floor, earnings_ceil, coef_a, coef_b, "
        " effective_from, effective_to, source_doc) "
        "VALUES "
        "(:period, :floor, :ceil, :a, :b, :ef_from, :ef_to, :src)"
    )
    for row in _STSL_TFT_WEEKLY:
        floor, ceil, a, b = row
        conn.execute(
            insert_stsl,
            {
                "period": "WEEKLY",
                "floor": floor,
                "ceil": ceil,
                "a": a,
                "b": b,
                "ef_from": _STSL_START,
                "ef_to": _FY25_26_END,
                "src": _SRC_NAT3539,
            },
        )


# --------------------------------------------------------------------- #
# downgrade                                                             #
# --------------------------------------------------------------------- #


def downgrade() -> None:
    """Re-insert the original placeholder rows from 0113_payg_tables.

    This restores the pre-verification state. Richard has confirmed
    rollback is unlikely; this is provided for schema symmetry only.
    """
    conn = op.get_bind()

    conn.execute(sa.text("DELETE FROM payg_tax_scales"))
    conn.execute(sa.text("DELETE FROM stsl_coefficients"))

    insert_payg = sa.text(
        "INSERT INTO payg_tax_scales "
        "(scale_no, period, earnings_floor, earnings_ceil, coef_a, coef_b, "
        " effective_from, effective_to, source_doc) "
        "VALUES "
        "(:scale_no, 'WEEKLY', :floor, :ceil, :a, :b, :ef_from, :ef_to, :src)"
    )
    for scale_no, bands in _PLACEHOLDER_SCALES.items():
        src = _PLACEHOLDER_WHM_SRC if scale_no == 7 else _PLACEHOLDER_SRC
        for row in bands:
            floor, ceil, a, b = row
            conn.execute(
                insert_payg,
                {
                    "scale_no": scale_no,
                    "floor": floor,
                    "ceil": ceil,
                    "a": a,
                    "b": b,
                    "ef_from": _FY25_26_START,
                    "ef_to": _FY25_26_END,
                    "src": src,
                },
            )

    insert_stsl = sa.text(
        "INSERT INTO stsl_coefficients "
        "(period, earnings_floor, earnings_ceil, coef_a, coef_b, "
        " effective_from, effective_to, source_doc) "
        "VALUES "
        "(:period, :floor, :ceil, :a, :b, :ef_from, :ef_to, :src)"
    )
    for row in _PLACEHOLDER_STSL:
        floor, ceil, a, b = row
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
                "src": _PLACEHOLDER_STSL_SRC,
            },
        )
