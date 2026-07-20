"""Tests for ``saebooks.services.fringe_benefits_ee``.

kmd-inf-tsd follow-up, Packet 2 (fringe-benefit compute). No DB required
— ``REFERENCE_DATABASE_URL`` is never configured in this test harness
(only ``REFERENCE_MIGRATION_DATABASE_URL``), so every call here resolves
via the embedded-fallback path, same posture as ``test_payroll_ee.py``.

The EMTA worked-example golden (``test_emta_worked_example_960``) is the
formula-correctness anchor: it reproduces EMTA's own published figures
exactly, including the rounding ORDER (income tax rounded to 2dp before
it's added into the social-tax base) — see the module docstring's
citation. The company-car goldens are this packet's own hand-computed
cases, built on that same verified formula.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from saebooks.services.fringe_benefits_ee import (
    FringeBenefitEEError,
    compute_car_fringe_benefit,
    compute_cash_fringe_benefit,
)

_EFFECTIVE = date(2026, 4, 30)


async def test_emta_worked_example_960() -> None:
    """EMTA's own published example (fringe-benefits page, verified
    2026-07-11): a EUR 960 taxable value -> income tax EUR 270.77,
    social tax EUR 406.15. Generic cash-value path (no car valuation
    step) since EMTA's example does not name a kW figure."""
    result = await compute_cash_fringe_benefit(
        benefit_category="motor_vehicle", taxable_value=Decimal("960"),
        effective_date=_EFFECTIVE,
    )
    assert result.taxable_value == Decimal("960.00")
    assert result.income_tax == Decimal("270.77")
    assert result.social_tax == Decimal("406.15")
    assert result.total_employer_cost == Decimal("676.92")
    assert result.source == "embedded_fallback"


async def test_company_car_standard_rate_hand_computed() -> None:
    """110 kW, 2 years old -> standard rate (1.96 EUR/kW/month).

    Hand-computed: value = 110 x 1.96 = 215.60. Income tax =
    215.60 x 22/78 = 60.8102... -> EUR 60.81. Social tax =
    (215.60 + 60.81) x 33% = 276.41 x 0.33 = 91.2153 -> EUR 91.22.
    """
    result = await compute_car_fringe_benefit(
        engine_power_kw=Decimal("110"), car_age_years=2, effective_date=_EFFECTIVE,
    )
    assert result.benefit_category == "motor_vehicle"
    assert result.rate_used_eur_per_kw == Decimal("1.96")
    assert result.taxable_value == Decimal("215.60")
    assert result.income_tax == Decimal("60.81")
    assert result.social_tax == Decimal("91.22")
    assert result.total_employer_cost == Decimal("152.03")
    assert result.engine_power_kw == Decimal("110")
    assert result.car_age_years == 2
    assert result.source == "embedded_fallback/embedded_fallback"


async def test_company_car_aged_rate_over_five_years_hand_computed() -> None:
    """110 kW, 6 years old -> aged rate (1.47 EUR/kW/month, car older
    than 5 years).

    Hand-computed: value = 110 x 1.47 = 161.70. Income tax =
    161.70 x 22/78 = 45.6076... -> EUR 45.61. Social tax =
    (161.70 + 45.61) x 33% = 207.31 x 0.33 = 68.4123 -> EUR 68.41.
    """
    result = await compute_car_fringe_benefit(
        engine_power_kw=Decimal("110"), car_age_years=6, effective_date=_EFFECTIVE,
    )
    assert result.rate_used_eur_per_kw == Decimal("1.47")
    assert result.taxable_value == Decimal("161.70")
    assert result.income_tax == Decimal("45.61")
    assert result.social_tax == Decimal("68.41")
    assert result.total_employer_cost == Decimal("114.02")


async def test_company_car_exactly_five_years_uses_standard_rate() -> None:
    """EMTA's own wording is 'older than five years' — a car exactly 5
    years old is NOT yet older than 5, so it still gets the standard
    rate (boundary case, not >=5)."""
    result = await compute_car_fringe_benefit(
        engine_power_kw=Decimal("110"), car_age_years=5, effective_date=_EFFECTIVE,
    )
    assert result.rate_used_eur_per_kw == Decimal("1.96")
    assert result.taxable_value == Decimal("215.60")


async def test_car_negative_kw_refused() -> None:
    with pytest.raises(FringeBenefitEEError, match="engine_power_kw"):
        await compute_car_fringe_benefit(
            engine_power_kw=Decimal("-1"), car_age_years=1, effective_date=_EFFECTIVE,
        )


async def test_car_negative_age_refused() -> None:
    with pytest.raises(FringeBenefitEEError, match="car_age_years"):
        await compute_car_fringe_benefit(
            engine_power_kw=Decimal("100"), car_age_years=-1, effective_date=_EFFECTIVE,
        )


async def test_cash_negative_value_refused() -> None:
    with pytest.raises(FringeBenefitEEError, match="taxable_value"):
        await compute_cash_fringe_benefit(
            benefit_category="housing", taxable_value=Decimal("-1"),
            effective_date=_EFFECTIVE,
        )


async def test_cash_empty_category_refused() -> None:
    with pytest.raises(FringeBenefitEEError, match="benefit_category"):
        await compute_cash_fringe_benefit(
            benefit_category="", taxable_value=Decimal("100"),
            effective_date=_EFFECTIVE,
        )
