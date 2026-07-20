"""LV payroll engine unit tests — the flat-25.5% monthly model, the
allowance boundaries, the VSAOI pair, the JE-balance invariant, and the
hard refusals.

THE MODEL PIN: Latvian monthly withholding is a single flat 25.5% —
the 33% band (annual income over EUR 105,300 = EUR 8,775/month) and the
3% surcharge settle via the ANNUAL return, never payroll. The
high-earner case below asserts the flat rate holds ABOVE the monthly
band-equivalent, which is exactly where a wrongly-ported banded model
would diverge.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from saebooks.jurisdictions.lv.payroll import (
    LVPayrollEngine,
    LVPayrollUnsupported,
)
from saebooks.services.payroll.types import (
    PayrollComponentRole,
    PayrollContext,
)


def _ctx(gross: str, *, lv: dict | None = ..., period: str = "MONTHLY",
         deductions: str = "0") -> PayrollContext:
    extra = {}
    if lv is ...:
        extra = {"lv": {"tax_book_submitted": True}}
    elif lv is not None:
        extra = {"lv": lv}
    return PayrollContext(
        company_id=uuid.uuid4(),
        employee_id=uuid.uuid4(),
        pay_run_id=uuid.uuid4(),
        period=period,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
        effective_date=date(2026, 3, 31),
        gross=Decimal(gross),
        ote=Decimal(gross),
        deductions_total=Decimal(deductions),
        extra=extra,
    )


def _totals(result):
    return {
        "iin_plus_ee_vsaoi": result.total_for(PayrollComponentRole.WITHHOLDING_LIABILITY),
        "er_liab": result.total_for(PayrollComponentRole.EMPLOYER_SOCIAL_LIABILITY),
        "er_exp": result.total_for(PayrollComponentRole.EMPLOYER_SOCIAL_EXPENSE),
        "retirement": result.total_for(PayrollComponentRole.RETIREMENT_LIABILITY),
    }


async def test_lv_standard_case_with_tax_book_and_dependant() -> None:
    """gross 2000, tax book, 1 dependant:
    ee VSAOI 210.00; base = 2000-210-550-250 = 990; IIN = 252.45;
    er VSAOI 471.80; net = 2000-210-252.45 = 1537.55."""
    r = await LVPayrollEngine().compute_line(
        None, _ctx("2000.00", lv={"tax_book_submitted": True, "dependants": 1})
    )
    t = _totals(r)
    assert t["iin_plus_ee_vsaoi"] == Decimal("252.45") + Decimal("210.00")
    assert t["er_liab"] == t["er_exp"] == Decimal("471.80")
    assert t["retirement"] == Decimal("0")  # pillar II is a state-side redirect
    assert r.net == Decimal("1537.55")


async def test_lv_no_tax_book_keeps_rate_but_drops_allowances() -> None:
    """The verified mechanic: no tax book -> rate STAYS 25.5% (not a
    punitive band), but no minimum/allowances — tax from the first euro.
    gross 2000: base = 2000-210 = 1790; IIN = 456.45; net = 1333.55."""
    r = await LVPayrollEngine().compute_line(
        None, _ctx("2000.00", lv={"tax_book_submitted": False})
    )
    assert r.total_for(PayrollComponentRole.WITHHOLDING_LIABILITY) == (
        Decimal("456.45") + Decimal("210.00")
    )
    assert r.net == Decimal("1333.55")


async def test_lv_allowance_boundary_zero_taxable_base() -> None:
    """gross 600 with tax book: base = max(0, 600-63-550) = 0 -> IIN 0;
    only VSAOI is withheld."""
    r = await LVPayrollEngine().compute_line(
        None, _ctx("600.00", lv={"tax_book_submitted": True})
    )
    assert r.total_for(PayrollComponentRole.WITHHOLDING_LIABILITY) == Decimal("63.00")
    assert r.net == Decimal("537.00")


async def test_lv_minimum_wage_case() -> None:
    """gross 780 (the 2026 minimum wage): ee 81.90; base 148.10;
    IIN 37.77; net 660.33."""
    r = await LVPayrollEngine().compute_line(
        None, _ctx("780.00", lv={"tax_book_submitted": True})
    )
    assert r.total_for(PayrollComponentRole.WITHHOLDING_LIABILITY) == (
        Decimal("37.77") + Decimal("81.90")
    )
    assert r.net == Decimal("660.33")


async def test_lv_high_earner_stays_flat_25_5_above_the_annual_band_equivalent() -> None:
    """gross 10,000/month (> EUR 8,775 = 105,300/12): the flat model
    gives base = 10000-1050-550 = 8400, IIN = 2142.00. A wrongly-ported
    monthly-banded model (25.5% to 8,775 + 33% above) would produce a
    DIFFERENT number — this is the divergence pin."""
    r = await LVPayrollEngine().compute_line(
        None, _ctx("10000.00", lv={"tax_book_submitted": True})
    )
    iin = next(
        c for c in r.components
        if c.role is PayrollComponentRole.WITHHOLDING_LIABILITY and "IIN" in c.note
    )
    assert iin.amount == Decimal("2142.00")
    assert "annual return" in iin.note
    assert r.net == Decimal("6808.00")
    # VSAOI keeps applying at full rate above the cap-equivalent too
    # (solidarity tax mechanics — VID reconciles after year-end).
    assert r.total_for(PayrollComponentRole.EMPLOYER_SOCIAL_LIABILITY) == Decimal("2359.00")


async def test_lv_payroll_line_balances_for_je_posting() -> None:
    """The invariant finalize_with_je's role-tag posting relies on:
    gross + expense components == liability components + net."""
    r = await LVPayrollEngine().compute_line(
        None,
        _ctx("3456.78", lv={"tax_book_submitted": True, "dependants": 2},
             deductions="50.00"),
    )
    debits = r.gross + sum(
        (c.amount for c in r.components if c.role.posts_debit), start=Decimal("0")
    )
    credits = r.net + sum(
        (c.amount for c in r.components if not c.role.posts_debit), start=Decimal("0")
    )
    # Caller-supplied deductions (50.00) are carved out of net but posted
    # by the CORE's own deduction leg, not an engine component — so the
    # engine-side identity is debits == credits + deductions.
    assert debits == credits + Decimal("50.00")


# ---------------------------------------------------------------------------
# Hard refusals — never a silent wrong number.
# ---------------------------------------------------------------------------


async def test_lv_missing_lv_block_refused() -> None:
    with pytest.raises(LVPayrollUnsupported, match="extra\\['lv'\\]"):
        await LVPayrollEngine().compute_line(None, _ctx("1000.00", lv=None))


async def test_lv_missing_tax_book_flag_refused() -> None:
    with pytest.raises(LVPayrollUnsupported, match="tax_book_submitted"):
        await LVPayrollEngine().compute_line(None, _ctx("1000.00", lv={}))


@pytest.mark.parametrize("period", ["WEEKLY", "FORTNIGHTLY", "FOUR_WEEKLY"])
async def test_lv_non_monthly_frequency_refused(period: str) -> None:
    with pytest.raises(LVPayrollUnsupported, match="MONTHLY"):
        await LVPayrollEngine().compute_line(
            None, _ctx("1000.00", period=period)
        )


@pytest.mark.parametrize(
    "flags",
    [
        {"tax_book_submitted": True, "pensioner": True},
        {"tax_book_submitted": True, "disability_pension": True},
        {"tax_book_submitted": True, "micro_enterprise": True},
        {"tax_book_submitted": True, "board_member": True},
        {"tax_book_submitted": True, "royalty_regime": True},
    ],
)
async def test_lv_unsupported_employee_categories_refused(flags: dict) -> None:
    with pytest.raises(LVPayrollUnsupported):
        await LVPayrollEngine().compute_line(None, _ctx("1000.00", lv=flags))
