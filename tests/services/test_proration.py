"""Pure-math tests for saebooks/services/proration.py.

These run without DB / FastAPI — just the math primitives. The HTTP-tier
tests for /api/v1/proration/* live in tests/api/v1/test_proration.py and
exercise the same primitives through the router.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from saebooks.services.proration import (
    ProrateBasis,
    ProrationError,
    days_in_basis_period,
    days_inclusive,
    first_period_prorate,
    plan_change_adjustment,
    prorate_amount,
    prorate_factor,
)

# --------------------------------------------------------------------------- #
# days_in_basis_period
# --------------------------------------------------------------------------- #


def test_days_in_basis_daily() -> None:
    assert days_in_basis_period(ProrateBasis.DAILY, date(2026, 1, 1)) == 1


def test_days_in_basis_weekly() -> None:
    assert days_in_basis_period(ProrateBasis.WEEKLY, date(2026, 1, 1)) == 7


def test_days_in_basis_monthly_31() -> None:
    assert days_in_basis_period(ProrateBasis.MONTHLY, date(2026, 1, 15)) == 31


def test_days_in_basis_monthly_february_non_leap() -> None:
    assert days_in_basis_period(ProrateBasis.MONTHLY, date(2026, 2, 1)) == 28


def test_days_in_basis_monthly_february_leap() -> None:
    assert days_in_basis_period(ProrateBasis.MONTHLY, date(2024, 2, 1)) == 29


def test_days_in_basis_quarterly_q1() -> None:
    # Jan 31 + Feb 28 + Mar 31 = 90
    assert days_in_basis_period(ProrateBasis.QUARTERLY, date(2026, 1, 1)) == 90


def test_days_in_basis_annual_365() -> None:
    assert days_in_basis_period(ProrateBasis.ANNUAL, date(2026, 3, 1)) == 365


def test_days_in_basis_annual_through_leap_day() -> None:
    # 1-Mar-2023 through 1-Mar-2024 traverses 29-Feb-2024 → 366
    assert days_in_basis_period(ProrateBasis.ANNUAL, date(2023, 3, 1)) == 366


# --------------------------------------------------------------------------- #
# days_inclusive
# --------------------------------------------------------------------------- #


def test_days_inclusive_one_day() -> None:
    assert days_inclusive(date(2026, 1, 1), date(2026, 1, 1)) == 1


def test_days_inclusive_full_month() -> None:
    assert days_inclusive(date(2026, 1, 1), date(2026, 1, 31)) == 31


def test_days_inclusive_end_before_start_raises() -> None:
    with pytest.raises(ProrationError):
        days_inclusive(date(2026, 2, 1), date(2026, 1, 31))


# --------------------------------------------------------------------------- #
# prorate_amount — Prorate flow #3 (per-line date-range)
# --------------------------------------------------------------------------- #


def test_prorate_amount_full_month_returns_full() -> None:
    result = prorate_amount(
        Decimal("3000"), ProrateBasis.MONTHLY, date(2026, 1, 1), date(2026, 1, 31)
    )
    assert result == Decimal("3000.00")


def test_prorate_amount_partial_month_rounds_correctly() -> None:
    # 13 of 31 days @ $3000/month = 3000 × 13 / 31 = 1258.0645... → 1258.06
    result = prorate_amount(
        Decimal("3000"), ProrateBasis.MONTHLY, date(2026, 1, 1), date(2026, 1, 13)
    )
    assert result == Decimal("1258.06")


def test_prorate_amount_no_factor_rounding_leak() -> None:
    """Going through prorate_factor (4dp) would leak ~14c on this case;
    the direct ``days × amount / full`` path stays at 1258.06."""
    via_amount = prorate_amount(
        Decimal("3000"), ProrateBasis.MONTHLY, date(2026, 1, 1), date(2026, 1, 13)
    )
    factor = prorate_factor(
        ProrateBasis.MONTHLY, date(2026, 1, 1), date(2026, 1, 13)
    )
    via_factor_then_round = (Decimal("3000") * factor).quantize(Decimal("0.01"))
    assert via_amount == Decimal("1258.06")
    # The factor-then-round path is the leaky one — confirm it differs
    # so anyone "simplifying" prorate_amount notices the regression.
    assert via_factor_then_round != via_amount


def test_prorate_amount_annual_basis() -> None:
    # 31 days of 365-day year @ $3650 = 3650 × 31 / 365 = 310.00
    result = prorate_amount(
        Decimal("3650"), ProrateBasis.ANNUAL, date(2026, 1, 1), date(2026, 1, 31)
    )
    assert result == Decimal("310.00")


# --------------------------------------------------------------------------- #
# first_period_prorate — Prorate flow #1
# --------------------------------------------------------------------------- #


def test_first_period_prorate_mid_month() -> None:
    # Sign-up 18-Apr, period through 30-Apr → 13 of 30 days @ $99
    result = first_period_prorate(
        Decimal("99"), ProrateBasis.MONTHLY, date(2026, 4, 18), date(2026, 4, 30)
    )
    assert result.days_used == 13
    assert result.days_in_full == 30
    # 99 × 13 / 30 = 42.9
    assert result.prorated_amount == Decimal("42.90")


def test_first_period_prorate_full_period() -> None:
    result = first_period_prorate(
        Decimal("99"), ProrateBasis.MONTHLY, date(2026, 4, 1), date(2026, 4, 30)
    )
    assert result.prorated_amount == Decimal("99.00")
    assert result.factor == Decimal("1.0000")


# --------------------------------------------------------------------------- #
# plan_change_adjustment — Prorate flow #2
# --------------------------------------------------------------------------- #


def test_plan_change_midmonth_upgrade() -> None:
    # $99 → $149 on day 16 of a 30-day month (1-Apr to 30-Apr).
    # days_used = 15 (Apr 1..15), days_remaining = 15.
    # credit = 99 × 15/30 = 49.50; charge = 149 × 15/30 = 74.50.
    adj = plan_change_adjustment(
        Decimal("99"),
        Decimal("149"),
        date(2026, 4, 1),
        date(2026, 4, 30),
        date(2026, 4, 16),
    )
    assert adj.days_total == 30
    assert adj.days_used == 15
    assert adj.days_remaining == 15
    assert adj.credit == Decimal("49.50")
    assert adj.charge == Decimal("74.50")
    assert adj.net == Decimal("25.00")


def test_plan_change_first_day() -> None:
    # change_date == period_start → all 30 days remaining (no credit reduction)
    adj = plan_change_adjustment(
        Decimal("99"),
        Decimal("149"),
        date(2026, 4, 1),
        date(2026, 4, 30),
        date(2026, 4, 1),
    )
    assert adj.days_used == 0
    assert adj.days_remaining == 30
    assert adj.credit == Decimal("99.00")
    assert adj.charge == Decimal("149.00")


def test_plan_change_last_day() -> None:
    # change_date == period_end → only 1 day on the new plan
    adj = plan_change_adjustment(
        Decimal("99"),
        Decimal("149"),
        date(2026, 4, 1),
        date(2026, 4, 30),
        date(2026, 4, 30),
    )
    assert adj.days_used == 29
    assert adj.days_remaining == 1
    # credit = 99 × 1/30 = 3.30 ; charge = 149 × 1/30 = 4.97
    assert adj.credit == Decimal("3.30")
    assert adj.charge == Decimal("4.97")


def test_plan_change_downgrade_negative_net() -> None:
    # $149 → $99 — net should be negative (refund situation)
    adj = plan_change_adjustment(
        Decimal("149"),
        Decimal("99"),
        date(2026, 4, 1),
        date(2026, 4, 30),
        date(2026, 4, 16),
    )
    assert adj.net < Decimal("0")


def test_plan_change_change_outside_period_raises() -> None:
    with pytest.raises(ProrationError):
        plan_change_adjustment(
            Decimal("99"),
            Decimal("149"),
            date(2026, 4, 1),
            date(2026, 4, 30),
            date(2026, 5, 1),  # outside period
        )
