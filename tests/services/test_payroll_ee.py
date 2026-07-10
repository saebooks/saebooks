"""Tests for ``saebooks.services.payroll_ee``.

kmd-inf-tsd scope Packet 3 golden month (``~/.claude/plans/kmd-inf-tsd-
scope.md`` §6): E1 crosses the EUR 886 social-tax wage-base floor, E2
elects pillar-II 6%. No DB required — ``REFERENCE_DATABASE_URL`` is
never configured in this test harness (only
``REFERENCE_MIGRATION_DATABASE_URL``), so ``compute_ee_payroll`` always
resolves via the embedded-fallback path here; that path IS what
production runs whenever the reference DB is absent/unseeded, so this
is a real exercise of real production code, not a mock.

Both golden employees are assumed to have filed the avaldus electing
this employer to apply their basic exemption (the scope's own worked
figures assume the EUR 700 exemption applies) — so
``basic_exemption_elected=True`` is passed explicitly on every golden
call; the module's actual default (unset -> NOT applied) is exercised
separately by ``test_basic_exemption_default_is_not_applied``.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from saebooks.services.payroll_ee import PayrollEEError, compute_ee_payroll

_EFFECTIVE = date(2026, 4, 30)


async def test_e1_low_wage_crosses_social_tax_floor() -> None:
    """Gross EUR 500/mo, pillar-II left at the 2% default, exemption
    elected.

    Income tax: (500 - 700 exemption - 8 unemployment - 10 pillar-II)
    < 0 -> EUR 0.00. Social tax base is max(500, 886) = 886 ->
    EUR 292.38 (886 x 33%), NOT EUR 165.00 — the floor is the point of
    this scenario (proves ``wage_base_floor``, not gross, drives it).
    """
    result = await compute_ee_payroll(
        gross=Decimal("500.00"), effective_date=_EFFECTIVE,
        basic_exemption_elected=True,
    )
    assert result.gross == Decimal("500.00")
    assert result.unemployment_employee == Decimal("8.00")
    assert result.unemployment_employer == Decimal("4.00")
    assert result.pillar_ii == Decimal("10.00")
    assert result.pillar_ii_rate_percent == Decimal("2.0")
    assert result.income_tax == Decimal("0.00")
    assert result.social_tax_base == Decimal("886.00")
    assert result.social_tax == Decimal("292.38")
    assert result.source == "embedded_fallback"


async def test_e2_pillar_ii_6pct_election() -> None:
    """Gross EUR 2,000/mo, pillar-II elected at 6%, exemption elected.

    Pillar II = EUR 120.00 (2,000 x 6%) — proves the elective rate
    flows through. Unemployment employee EUR 32.00 / employer
    EUR 16.00. Social tax EUR 660.00 (gross exceeds the floor, so the
    floor does not bite here).
    """
    result = await compute_ee_payroll(
        gross=Decimal("2000.00"),
        effective_date=_EFFECTIVE,
        pillar_ii_rate_percent=Decimal("6.0"),
        basic_exemption_elected=True,
    )
    assert result.pillar_ii == Decimal("120.00")
    assert result.pillar_ii_rate_percent == Decimal("6.0")
    assert result.unemployment_employee == Decimal("32.00")
    assert result.unemployment_employer == Decimal("16.00")
    assert result.social_tax_base == Decimal("2000.00")
    assert result.social_tax == Decimal("660.00")
    # Income tax ordering (UNVERIFIED, see module docstring): taxable =
    # 2000 - 700 - 32 - 120 = 1148; 1148 x 22% = 252.56.
    assert result.income_tax == Decimal("252.56")


async def test_basic_exemption_default_is_not_applied() -> None:
    """Tax-safe default: an unset ``basic_exemption_elected`` does NOT
    apply the exemption (an employee must have filed the avaldus with
    THIS employer) — mirrors ``Employee.ee_basic_exemption_elected``
    NULL semantics."""
    result = await compute_ee_payroll(
        gross=Decimal("2000.00"), effective_date=_EFFECTIVE,
    )
    assert result.basic_exemption_applied == Decimal("0")


async def test_basic_exemption_elected_true_applies_standard_amount() -> None:
    with_exemption = await compute_ee_payroll(
        gross=Decimal("2000.00"), effective_date=_EFFECTIVE,
        basic_exemption_elected=True,
    )
    without_exemption = await compute_ee_payroll(
        gross=Decimal("2000.00"), effective_date=_EFFECTIVE,
        basic_exemption_elected=False,
    )
    assert with_exemption.basic_exemption_applied == Decimal("700.00")
    assert without_exemption.basic_exemption_applied == Decimal("0")
    assert without_exemption.income_tax > with_exemption.income_tax


async def test_pensionable_age_uses_776_exemption() -> None:
    """``pensionable_age=True`` selects the EUR 776/mo figure (code
    650) instead of the standard EUR 700/mo (code 610) when the
    exemption is elected — a higher exemption than the standard case,
    proving the flag actually swaps the rate row."""
    standard = await compute_ee_payroll(
        gross=Decimal("2000.00"), effective_date=_EFFECTIVE,
        basic_exemption_elected=True, pensionable_age=False,
    )
    pensionable = await compute_ee_payroll(
        gross=Decimal("2000.00"), effective_date=_EFFECTIVE,
        basic_exemption_elected=True, pensionable_age=True,
    )
    assert standard.basic_exemption_applied == Decimal("700.00")
    assert pensionable.basic_exemption_applied == Decimal("776.00")
    assert pensionable.income_tax < standard.income_tax

    # pensionable_age is ignored when the exemption isn't elected at all.
    not_elected = await compute_ee_payroll(
        gross=Decimal("2000.00"), effective_date=_EFFECTIVE,
        pensionable_age=True,
    )
    assert not_elected.basic_exemption_applied == Decimal("0")


async def test_rejects_invalid_pillar_rate() -> None:
    with pytest.raises(PayrollEEError):
        await compute_ee_payroll(
            gross=Decimal("1000.00"),
            effective_date=_EFFECTIVE,
            pillar_ii_rate_percent=Decimal("3.0"),
        )


async def test_rejects_negative_gross() -> None:
    with pytest.raises(PayrollEEError):
        await compute_ee_payroll(gross=Decimal("-1.00"), effective_date=_EFFECTIVE)


async def test_social_tax_floor_does_not_apply_above_it() -> None:
    """A gross well above EUR 886 uses gross itself as the social-tax
    base, not the floor."""
    result = await compute_ee_payroll(
        gross=Decimal("1200.00"), effective_date=_EFFECTIVE,
    )
    assert result.social_tax_base == Decimal("1200.00")
    assert result.social_tax == Decimal("396.00")
