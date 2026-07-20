"""PAYG withholding calculation engine.

Implements the ATO Schedule 1 (NAT 1004) "statement of formulas for
calculating amounts to be withheld" against the ``payg_tax_scales``
and ``stsl_coefficients`` reference tables.

The exported entry point is :func:`compute_withholding`. Everything
else in the module is internal.

Formula (NAT 1004 §2):

    1. Round the weekly equivalent of period_gross **down** to whole
       dollars, then add 0.99 cents:

           x = floor(weekly_gross) + 0.99

    2. Look up the row in payg_tax_scales matching:
         - the resolved scale_no (1–8) based on the employee's flags
         - period = WEEKLY (we always normalise to weekly)
         - earnings_floor <= x < earnings_ceil (or no ceil for top band)
         - effective_from <= effective_date <= effective_to

    3. Withholding amount per week:

           WH_weekly = round(coef_a * x - coef_b)

       per NAT 1004 §6: half-cent rounds **up**, i.e. AWAY_FROM_ZERO
       at the half. Result is whole-dollar.

    4. STSL top-up (if employee.study_training_support_loan):

           STSL_weekly = round(stsl_a * x - stsl_b)

       Combined withholding = WH_weekly + STSL_weekly.

    5. Scale back to the requested period:

           WH_period = WH_weekly * period_multiplier

       (FORTNIGHTLY = ×2, MONTHLY = ×13/3, WEEKLY = ×1.)

Scale resolution (resolves to a single ``scale_no`` 1–8, matching
ATO Schedule 1 NAT 1004 numbering since migration 0120):

    +-----------------------------------------------+----------+
    | Employee state                                | scale_no |
    +-----------------------------------------------+----------+
    | WHM (working_holiday_maker = True)            |    7     |
    | TFN not provided, resident                    |    4     |
    | TFN not provided, non-resident                |    8     |
    | Resident, full medicare exemption             |    5     |
    | Resident, half medicare exemption             |    6     |
    | Resident, no TFT                              |    1     |
    | Resident, claims TFT                          |    2     |
    | Non-resident (claiming any TFT flag)          |    3     |
    +-----------------------------------------------+----------+

The "no TFN" scales (4 / 8) override every other flag — once a TFN is
not provided, the flat 47% / 45% (no MC) rate applies regardless of
residency-or-TFT settings on the rest of the row.

WHM overrides the no-TFN flag too (per ATO: a WHM without a TFN is
still taxed at the WHM schedule, not the flat 47%).

Returns a :class:`WithholdingResult` with the cents-precision payg
amount, the scale used, and a human-readable breakdown.

NOT IN SCOPE for this module:
    * Tax offsets (ATO Schedule 7 / 8 — claimed offsets reduce WH)
    * HELP foreign-resident additional WH
    * Voluntary additional withholding
    * Lump-sum / back-pay formula (NAT 3348 schedule 5 — Phase 2B)
    * Bonus & commission schedule (NAT 7905 schedule 5)
    * Medicare levy variation (form NAT 0929 — half/full exemption is
      handled via Scale 5/6 selection, but the variation form
      *value* is not stored on Employee yet)

These will be layered in as additional services and tested against
their own ATO worked-example tables.
"""
from __future__ import annotations

import dataclasses
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.employee import Employee, PayFrequency, TfnStatus
from saebooks.models.payg import PaygTaxScale, StslCoefficient
from saebooks.money import round_money

# --------------------------------------------------------------------- #
# Public dataclasses                                                    #
# --------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class WithholdingResult:
    """Outcome of a withholding calculation for a single pay-line."""

    payg_amount: Decimal
    """Total PAYG withholding for the period, in dollars (2 dp)."""

    scale_used: int
    """Resolved ATO scale (1–8 per the internal mapping)."""

    stsl_amount: Decimal
    """STSL component (subset of payg_amount). Zero if not flagged."""

    base_payg_amount: Decimal
    """PAYG minus STSL — i.e. the base-scale-only withholding."""

    weekly_equivalent: Decimal
    """Weekly equivalent gross used for the table lookup."""

    formula_x: Decimal
    """The ``x = floor(weekly) + 0.99`` value used in WH = a*x - b."""

    coef_a: Decimal
    coef_b: Decimal
    breakdown_note: str
    """Human-friendly explanation, e.g. "Scale 2 + STSL = $X"."""


# --------------------------------------------------------------------- #
# Scale resolution                                                      #
# --------------------------------------------------------------------- #


# ATO scale numbers (Schedule 1 NAT 1004 — the DB column ``scale_no``
# matches the ATO numbering after migration 0120; see the migration
# docstring for the historical inversion these constants now reflect).
_SCALE_RES_NO_TFT = 1
_SCALE_RES_TFT = 2
_SCALE_NONRES = 3
_SCALE_NO_TFN_RES = 4
_SCALE_FULL_MED_EXEMPT = 5
_SCALE_HALF_MED_EXEMPT = 6
_SCALE_WHM = 7
_SCALE_NO_TFN_NONRES = 8


MedicareExemption = Literal["FULL", "HALF", "NONE"]


def _resolve_scale(
    *,
    is_australian_resident: bool,
    claims_tax_free_threshold: bool,
    tfn_status: str,
    working_holiday_maker: bool,
    medicare_exemption: MedicareExemption = "NONE",
) -> int:
    """Map employee flags to internal scale_no (1–8)."""
    # WHM trumps every other consideration including no-TFN.
    if working_holiday_maker:
        return _SCALE_WHM

    no_tfn = tfn_status in (
        TfnStatus.NOT_PROVIDED.value,
        # NEW_PAYEE_30D is treated as "TFN supplied" for the first
        # 30 days — see plan G.3. After auto-flip (Phase 2B cron) the
        # status becomes NOT_PROVIDED and this branch fires.
    )
    if no_tfn:
        return (
            _SCALE_NO_TFN_RES if is_australian_resident
            else _SCALE_NO_TFN_NONRES
        )

    if not is_australian_resident:
        return _SCALE_NONRES

    if medicare_exemption == "FULL":
        return _SCALE_FULL_MED_EXEMPT
    if medicare_exemption == "HALF":
        return _SCALE_HALF_MED_EXEMPT

    return _SCALE_RES_TFT if claims_tax_free_threshold else _SCALE_RES_NO_TFT


# --------------------------------------------------------------------- #
# Period conversion                                                     #
# --------------------------------------------------------------------- #


_PERIOD_TO_WEEKLY: dict[str, Decimal] = {
    PayFrequency.WEEKLY.value: Decimal("1"),
    PayFrequency.FORTNIGHTLY.value: Decimal("0.5"),       # 1/2
    # ATO NAT 1004 §3.2: multiply monthly by 3 / 13 for weekly.
    PayFrequency.MONTHLY.value: Decimal("3") / Decimal("13"),
}
_WEEKLY_TO_PERIOD: dict[str, Decimal] = {
    PayFrequency.WEEKLY.value: Decimal("1"),
    PayFrequency.FORTNIGHTLY.value: Decimal("2"),
    # 13 / 3 — exact mirror of the inward conversion.
    PayFrequency.MONTHLY.value: Decimal("13") / Decimal("3"),
}


def _to_weekly(period_gross: Decimal, period: str) -> Decimal:
    """Convert a period gross to its weekly equivalent."""
    if period not in _PERIOD_TO_WEEKLY:
        raise ValueError(
            f"PAYG calc supports WEEKLY / FORTNIGHTLY / MONTHLY periods. "
            f"Got {period!r}."
        )
    return round_money(period_gross * _PERIOD_TO_WEEKLY[period])


def _from_weekly(weekly_wh: Decimal, period: str) -> Decimal:
    """Convert a weekly WH back to the period equivalent."""
    return round_money(weekly_wh * _WEEKLY_TO_PERIOD[period])


# --------------------------------------------------------------------- #
# Formula application                                                   #
# --------------------------------------------------------------------- #


def _formula_x(weekly_gross: Decimal) -> Decimal:
    """Compute ``x = floor(weekly_gross) + 0.99`` per NAT 1004 §2.

    The truncation is to whole dollars; the ``+ 0.99`` is a half-cent
    artifact so the published bands align with the published row
    boundaries (a band-edge of $865 means "earnings up to and
    including $865.99" — adding 0.99 lifts the rounded-down dollar
    onto the right side of every boundary).
    """
    whole_dollars = weekly_gross.to_integral_value(rounding="ROUND_FLOOR")
    return whole_dollars + Decimal("0.99")


def _round_whole_dollar(amount: Decimal) -> Decimal:
    """Round to whole dollars per NAT 1004 §6 (half-up)."""
    return amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------- #
# Lookups                                                               #
# --------------------------------------------------------------------- #


async def _lookup_band(
    session: AsyncSession,
    *,
    scale_no: int,
    period: str,
    x: Decimal,
    effective_date: date,
) -> PaygTaxScale:
    """Find the PAYG band row for (scale, period, x, date)."""
    stmt = (
        select(PaygTaxScale)
        .where(
            PaygTaxScale.scale_no == scale_no,
            PaygTaxScale.period == period,
            PaygTaxScale.effective_from <= effective_date,
            or_(
                PaygTaxScale.effective_to.is_(None),
                PaygTaxScale.effective_to >= effective_date,
            ),
            PaygTaxScale.earnings_floor <= x,
            or_(
                PaygTaxScale.earnings_ceil.is_(None),
                PaygTaxScale.earnings_ceil > x,
            ),
        )
        .order_by(PaygTaxScale.effective_from.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    band = result.scalars().first()
    if band is None:
        raise PaygDataMissing(
            f"No PAYG band found for scale_no={scale_no} period={period} "
            f"x={x} effective_date={effective_date}. "
            "Has the ATO Schedule 1 fixture been loaded for this FY?"
        )
    return band


async def _lookup_stsl_band(
    session: AsyncSession,
    *,
    period: str,
    x: Decimal,
    effective_date: date,
) -> StslCoefficient | None:
    """Find the STSL band row for (period, x, date). None = below threshold."""
    stmt = (
        select(StslCoefficient)
        .where(
            StslCoefficient.period == period,
            StslCoefficient.effective_from <= effective_date,
            or_(
                StslCoefficient.effective_to.is_(None),
                StslCoefficient.effective_to >= effective_date,
            ),
            StslCoefficient.earnings_floor <= x,
            or_(
                StslCoefficient.earnings_ceil.is_(None),
                StslCoefficient.earnings_ceil > x,
            ),
        )
        .order_by(StslCoefficient.effective_from.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalars().first()


# --------------------------------------------------------------------- #
# Public entry point                                                    #
# --------------------------------------------------------------------- #


class PaygDataMissing(LookupError):
    """Raised when no PAYG band row matches the request."""


async def compute_withholding(
    session: AsyncSession,
    *,
    gross_per_period: Decimal,
    period: str,
    employee: Employee,
    effective_date: date,
    medicare_exemption: MedicareExemption = "NONE",
) -> WithholdingResult:
    """Compute PAYG (+ STSL if applicable) withholding for one pay line.

    Parameters
    ----------
    session
        Async DB session — used to look up the band rows from
        ``payg_tax_scales`` / ``stsl_coefficients``.
    gross_per_period
        Gross taxable earnings for the pay period. Excludes super
        but **includes** taxable allowances / overtime / leave loading.
    period
        ``PayFrequency`` value — one of WEEKLY / FORTNIGHTLY /
        MONTHLY. Other periods (quarterly / annual) raise; payroll
        is paid at one of these three.
    employee
        The Employee row — flags determine scale resolution.
    effective_date
        The pay-period date (payment_date) for band selection.
    medicare_exemption
        Optional override — when an employee has lodged NAT 0929,
        pass ``FULL`` or ``HALF``. The flag is not yet stored on the
        Employee row (Phase 2B); for now, callers pass it explicitly.

    Returns
    -------
    WithholdingResult
        Cents-precision PAYG amount + the breakdown.
    """
    if gross_per_period < 0:
        raise ValueError("gross_per_period must be non-negative")
    # Coerce input to a 2 dp Decimal so callers can pass int / str.
    gross = round_money(Decimal(str(gross_per_period)))

    scale_no = _resolve_scale(
        is_australian_resident=employee.is_australian_resident,
        claims_tax_free_threshold=employee.claims_tax_free_threshold,
        tfn_status=employee.tfn_status,
        working_holiday_maker=employee.working_holiday_maker,
        medicare_exemption=medicare_exemption,
    )

    weekly_gross = _to_weekly(gross, period)
    x = _formula_x(weekly_gross)

    band = await _lookup_band(
        session,
        scale_no=scale_no,
        period=PayFrequency.WEEKLY.value,
        x=x,
        effective_date=effective_date,
    )

    wh_weekly_unrounded = band.coef_a * x - band.coef_b
    # NAT 1004 §6: weekly withholding is rounded to whole dollars
    # before period scaling. Half-up is the published rule.
    wh_weekly = _round_whole_dollar(
        wh_weekly_unrounded if wh_weekly_unrounded > 0 else Decimal("0")
    )

    # STSL top-up.
    stsl_weekly = Decimal("0")
    if employee.study_training_support_loan:
        stsl_band = await _lookup_stsl_band(
            session,
            period=PayFrequency.WEEKLY.value,
            x=x,
            effective_date=effective_date,
        )
        if stsl_band is not None and stsl_band.coef_a > 0:
            stsl_unrounded = stsl_band.coef_a * x - stsl_band.coef_b
            stsl_weekly = _round_whole_dollar(
                stsl_unrounded if stsl_unrounded > 0 else Decimal("0")
            )

    total_weekly = wh_weekly + stsl_weekly
    payg_period = _from_weekly(total_weekly, period)
    base_payg_period = _from_weekly(wh_weekly, period)
    stsl_period = _from_weekly(stsl_weekly, period)

    breakdown = (
        f"Scale {scale_no}"
        + (" + STSL" if stsl_weekly > 0 else "")
        + f"; weekly_gross=${weekly_gross} x=${x}"
        + f" a={band.coef_a} b={band.coef_b}"
        + f" wh_weekly=${wh_weekly}"
        + (f" stsl_weekly=${stsl_weekly}" if stsl_weekly > 0 else "")
        + f" -> period {period}=${payg_period}"
    )

    return WithholdingResult(
        payg_amount=payg_period,
        scale_used=scale_no,
        stsl_amount=stsl_period,
        base_payg_amount=base_payg_period,
        weekly_equivalent=weekly_gross,
        formula_x=x,
        coef_a=band.coef_a,
        coef_b=band.coef_b,
        breakdown_note=breakdown,
    )


# --------------------------------------------------------------------- #
# Sync wrappers for non-async callers (CLI, tests, payslip preview)     #
# --------------------------------------------------------------------- #


def resolve_scale_no(
    *,
    is_australian_resident: bool,
    claims_tax_free_threshold: bool,
    tfn_status: str,
    working_holiday_maker: bool,
    medicare_exemption: MedicareExemption = "NONE",
) -> int:
    """Expose scale resolution as a pure function for non-DB callers."""
    return _resolve_scale(
        is_australian_resident=is_australian_resident,
        claims_tax_free_threshold=claims_tax_free_threshold,
        tfn_status=tfn_status,
        working_holiday_maker=working_holiday_maker,
        medicare_exemption=medicare_exemption,
    )


def apply_formula(
    *,
    weekly_gross: Decimal,
    coef_a: Decimal,
    coef_b: Decimal,
) -> Decimal:
    """Apply ``WH = round(a*x - b)`` once a band has been looked up.

    Whole-dollar half-up rounding per NAT 1004 §6. Returns the
    weekly withholding. Use for unit tests / payslip-preview without
    a DB round trip.
    """
    x = _formula_x(weekly_gross)
    unrounded = coef_a * x - coef_b
    if unrounded < 0:
        return Decimal("0")
    return _round_whole_dollar(unrounded)


__all__ = [
    "MedicareExemption",
    "PaygDataMissing",
    "WithholdingResult",
    "apply_formula",
    "compute_withholding",
    "resolve_scale_no",
]
