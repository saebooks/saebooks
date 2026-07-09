"""Superannuation Guarantee (SG) contribution calculator.

Computes employer SG from OTE (Ordinary Times Earnings) per the
Superannuation Guarantee (Administration) Act 1992.

Key facts (FY25-26):

* **SG rate** = 12.0% from 1 July 2025 onwards.
* **Maximum super contribution base** (MSCB) = $65,070 per quarter
  FY25-26 — earnings above this cap don't attract SG. Indexed annually
  to AWOTE (Average Weekly Ordinary Time Earnings). Pre-indexation
  history kept in ``_HISTORICAL_MSCB`` so back-pay calcs against an
  older quarter use the right cap.
* **OTE** is the regular hours payment plus shift loading, commission,
  bonuses paid for ordinary hours, leave loading where contractual
  but **excludes**:
    - Overtime hours
    - Reimbursements (genuine expenses)
    - Termination payments
    - Lump-sum payments in lieu of unused leave on termination
  This module assumes the caller has already filtered to OTE — the
  ``ote`` parameter IS the OTE figure.
* **Rounding**: SG amounts are reported to the cent. The ATO does
  NOT mandate a specific rounding direction for the calculation
  itself (cf. PAYG which mandates whole-dollar at the weekly step);
  half-up to 2 dp is the industry default and matches the SBSCH
  output format.

References:
    - SGAA 1992 s 23(2) — quarterly maximum.
    - SGR 2009/2 — definition of OTE.
    - https://www.ato.gov.au/Rates/Key-superannuation-rates-and-thresholds/

Out of scope for this module:
    - Super on back-pay / lump sums (treat as OTE if for OT-period;
      caller decides — pass the correct ``ote`` figure).
    - Salary sacrifice (Phase 2B — sacrificed amount counts toward
      employer contribution but not toward SG obligation).
    - Award-prescribed contributions above 12% (caller must layer on
      top — see ``compute_super`` returning the *minimum* SG).
    - Workers under 18 working <30h/week (not legally required to
      pay SG — caller should short-circuit and not call this).
"""
from __future__ import annotations

import dataclasses
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

# --------------------------------------------------------------------- #
# Rate + cap history                                                    #
# --------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class SgRateBand:
    effective_from: date
    rate: Decimal


# SG rate schedule (legislated steps). Source: SGAA s 19.
# https://www.ato.gov.au/rates/key-superannuation-rates-and-thresholds/
_SG_RATE_HISTORY: list[SgRateBand] = [
    SgRateBand(date(2014, 7, 1), Decimal("0.0950")),  # 9.50%
    SgRateBand(date(2021, 7, 1), Decimal("0.1000")),  # 10.00%
    SgRateBand(date(2022, 7, 1), Decimal("0.1050")),  # 10.50%
    SgRateBand(date(2023, 7, 1), Decimal("0.1100")),  # 11.00%
    SgRateBand(date(2024, 7, 1), Decimal("0.1150")),  # 11.50%
    SgRateBand(date(2025, 7, 1), Decimal("0.1200")),  # 12.00% — final step
]


# Maximum super contribution base (per quarter). Indexed annually.
# Source: ATO key superannuation rates and thresholds.
_HISTORICAL_MSCB: dict[int, Decimal] = {
    # Financial year start (YYYY) → MSCB per quarter
    2022: Decimal("60220"),   # FY22-23
    2023: Decimal("62270"),   # FY23-24
    2024: Decimal("65070"),   # FY24-25
    2025: Decimal("65070"),   # FY25-26
    # ^ FY25-26 figure carries forward; ATO have not re-indexed for
    #   FY25-26 as the legislated SG cap freeze applies. Verify at:
    #   https://www.ato.gov.au/Rates/Key-superannuation-rates-and-thresholds/
}

# Fall-back when caller asks for a year we haven't recorded.
_DEFAULT_MSCB = Decimal("65070")


# --------------------------------------------------------------------- #
# Public types                                                          #
# --------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class SuperResult:
    """Outcome of an SG calculation for a single pay-line."""

    sg_amount: Decimal
    """SG contribution for the period, in dollars (2 dp)."""

    rate: Decimal
    """SG rate applied (decimal, e.g. 0.12 for 12%)."""

    ote_capped: Decimal
    """OTE figure actually used (possibly capped to MSCB)."""

    cap_applied: bool
    """True iff the input OTE was reduced by the cap."""

    period_cap: Decimal
    """The MSCB scaled to the input period."""

    breakdown_note: str
    """Human-friendly explanation."""


# --------------------------------------------------------------------- #
# Helpers                                                               #
# --------------------------------------------------------------------- #


_PERIODS_PER_QUARTER: dict[str, Decimal] = {
    # 13 weeks per quarter, 6.5 fortnights per quarter, 3 months per quarter.
    "WEEKLY": Decimal("13"),
    "FORTNIGHTLY": Decimal("6.5"),
    "MONTHLY": Decimal("3"),
}


def _resolve_rate(effective_date: date) -> Decimal:
    """Return the SG rate in force on ``effective_date``."""
    # Walk newest-to-oldest; first match wins.
    for band in reversed(_SG_RATE_HISTORY):
        if effective_date >= band.effective_from:
            return band.rate
    # Pre-2014 — not supported (the legislated rate was 9.25% then
    # 9.00%, but no caller should be running payroll that far back).
    raise ValueError(
        f"No SG rate defined for {effective_date} — supported from 1 Jul 2014."
    )


def _resolve_quarterly_cap(effective_date: date) -> Decimal:
    """Return the MSCB (quarterly cap) for the FY containing ``date``."""
    # FY runs 1 Jul YYYY → 30 Jun (YYYY+1).
    fy_start_year = (
        effective_date.year if effective_date.month >= 7
        else effective_date.year - 1
    )
    return _HISTORICAL_MSCB.get(fy_start_year, _DEFAULT_MSCB)


def _period_cap(effective_date: date, period: str) -> Decimal:
    """Scale the quarterly MSCB down to the pay period."""
    if period not in _PERIODS_PER_QUARTER:
        raise ValueError(
            "compute_super supports WEEKLY / FORTNIGHTLY / MONTHLY periods."
        )
    quarterly = _resolve_quarterly_cap(effective_date)
    per_period = quarterly / _PERIODS_PER_QUARTER[period]
    return per_period.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------- #
# Public entry point                                                    #
# --------------------------------------------------------------------- #


def compute_super(
    *,
    ote: Decimal,
    period: str,
    effective_date: date,
    max_base_quarter: Decimal | None = None,
) -> SuperResult:
    """Compute Super Guarantee for one pay-line's OTE.

    Parameters
    ----------
    ote
        Ordinary Times Earnings for the pay period — caller-filtered
        to exclude OT, reimbursements, termination payments etc.
    period
        WEEKLY / FORTNIGHTLY / MONTHLY.
    effective_date
        Date used to look up the in-force SG rate + MSCB.
    max_base_quarter
        Optional explicit cap override (dollars per quarter). Defaults
        to the published MSCB for the FY containing ``effective_date``.

    Returns
    -------
    SuperResult
        SG amount + the cap / rate used.

    Raises
    ------
    ValueError
        Negative OTE, unsupported period, or pre-2014 effective_date.
    """
    if ote < 0:
        raise ValueError("ote must be non-negative")
    ote = Decimal(str(ote)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    rate = _resolve_rate(effective_date)
    if max_base_quarter is not None:
        quarterly_cap = Decimal(str(max_base_quarter))
        period_cap = (
            quarterly_cap / _PERIODS_PER_QUARTER[period]
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    else:
        quarterly_cap = _resolve_quarterly_cap(effective_date)
        period_cap = _period_cap(effective_date, period)

    cap_applied = ote > period_cap
    ote_capped = period_cap if cap_applied else ote

    sg_amount = (ote_capped * rate).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP,
    )

    pct = (rate * Decimal("100")).quantize(Decimal("0.01"))
    breakdown = (
        f"SG {pct}% x OTE ${ote_capped}"
        + (
            f" (capped from ${ote} at period cap ${period_cap}; "
            f"quarterly cap ${quarterly_cap})"
            if cap_applied else ""
        )
        + f" = ${sg_amount}"
    )

    return SuperResult(
        sg_amount=sg_amount,
        rate=rate,
        ote_capped=ote_capped,
        cap_applied=cap_applied,
        period_cap=period_cap,
        breakdown_note=breakdown,
    )


def current_sg_rate(effective_date: date) -> Decimal:
    """Public read-only accessor for the SG rate."""
    return _resolve_rate(effective_date)


def quarterly_cap(effective_date: date) -> Decimal:
    """Public read-only accessor for the MSCB."""
    return _resolve_quarterly_cap(effective_date)


__all__ = [
    "SuperResult",
    "compute_super",
    "current_sg_rate",
    "quarterly_cap",
]
