"""UK payroll engine tests — cumulative PAYE (three nations), Class 1
NI band boundaries, student loans, auto-enrolment, the Employment
Allowance rule, and every hard-refusal path.

Pure compute tests: the engine is called directly with a synthetic
``PayrollContext`` (session=None — ``REFERENCE_DATABASE_URL`` is unset
in the test harness, so the embedded 2026-27 snapshot is what runs;
the payroll_ee convention). Expected figures are hand-computed from
the gov.uk rates-and-thresholds 2026-27 pull documented in the UK
seeds.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from saebooks.jurisdictions.uk.payroll import (
    EMPLOYMENT_ALLOWANCE_ANNUAL,
    UKPayrollEngine,
    UKPayrollUnsupported,
    employment_allowance_eligible,
)
from saebooks.services.payroll.types import (
    PayrollComponentRole,
    PayrollContext,
)


def _ctx(
    *,
    gross: str,
    period: str = "MONTHLY",
    uk: dict[str, Any] | None = None,
    deductions: str = "0",
) -> PayrollContext:
    return PayrollContext(
        company_id=uuid.uuid4(),
        employee_id=uuid.uuid4(),
        pay_run_id=uuid.uuid4(),
        period=period,
        period_start=date(2026, 4, 6),
        period_end=date(2026, 5, 5),
        effective_date=date(2026, 5, 5),
        gross=Decimal(gross),
        ote=Decimal(gross),
        deductions_total=Decimal(deductions),
        extra={} if uk is None else {"uk": uk},
    )


def _component_amounts(result, role):
    return [c.amount for c in result.components if c.role is role]


async def _run(ctx: PayrollContext):
    return await UKPayrollEngine().compute_line(None, ctx)


# ---------------------------------------------------------------------------
# PAYE — cumulative, three nations, flat codes, W1/M1.
# ---------------------------------------------------------------------------


async def test_paye_ruk_1257l_month_1() -> None:
    result = await _run(
        _ctx(gross="3000.00", uk={"tax_code": "1257L", "period_number": 1})
    )
    withholding = _component_amounts(result, PayrollComponentRole.WITHHOLDING_LIABILITY)
    # PAYE: free pay 12,579/12 = 1,048.25; taxable floor(1,951.75) =
    # 1,951 @ 20% = 390.20. Employee NI: (3,000 - 1,048) x 8% = 156.16.
    assert withholding[0] == Decimal("390.20")
    assert withholding[1] == Decimal("156.16")
    # Employer NI: (3,000 - 417) x 15% = 387.45, liability + expense legs.
    assert _component_amounts(result, PayrollComponentRole.EMPLOYER_SOCIAL_LIABILITY) == [Decimal("387.45")]
    assert _component_amounts(result, PayrollComponentRole.EMPLOYER_SOCIAL_EXPENSE) == [Decimal("387.45")]
    assert result.net == Decimal("2453.64")


async def test_paye_ruk_1257l_month_2_cumulative() -> None:
    result = await _run(
        _ctx(
            gross="3000.00",
            uk={
                "tax_code": "1257L",
                "period_number": 2,
                "ytd_taxable_pay": "3000.00",
                "ytd_tax_paid": "390.20",
            },
        )
    )
    # Free pay to date 2,096.50; taxable floor(3,903.50) = 3,903 @ 20%
    # = 780.60; minus 390.20 already paid = 390.40 this period.
    paye = _component_amounts(result, PayrollComponentRole.WITHHOLDING_LIABILITY)[0]
    assert paye == Decimal("390.40")


async def test_paye_scottish_s1257l_differs_from_ruk() -> None:
    result = await _run(
        _ctx(gross="3000.00", uk={"tax_code": "S1257L", "period_number": 1})
    )
    paye = _component_amounts(result, PayrollComponentRole.WITHHOLDING_LIABILITY)[0]
    # Scottish six-band: 330.58@19% + 1,082.42@20% + 538@21% = 392.27
    # (vs 390.20 rUK on the same gross).
    assert paye == Decimal("392.27")


async def test_paye_welsh_c1257l_parity_with_ruk() -> None:
    ruk = await _run(_ctx(gross="3000.00", uk={"tax_code": "1257L", "period_number": 1}))
    wales = await _run(_ctx(gross="3000.00", uk={"tax_code": "C1257L", "period_number": 1}))
    assert (
        _component_amounts(ruk, PayrollComponentRole.WITHHOLDING_LIABILITY)[0]
        == _component_amounts(wales, PayrollComponentRole.WITHHOLDING_LIABILITY)[0]
    )


async def test_paye_w1_non_cumulative_ignores_ytd() -> None:
    result = await _run(
        _ctx(
            gross="3000.00",
            uk={
                "tax_code": "1257L W1",
                "period_number": 5,
                "ytd_taxable_pay": "99999.00",
                "ytd_tax_paid": "0.00",
            },
        )
    )
    paye = _component_amounts(result, PayrollComponentRole.WITHHOLDING_LIABILITY)[0]
    assert paye == Decimal("390.20")   # single-period standalone


async def test_paye_flat_codes() -> None:
    for code, expected in (("BR", "600.00"), ("D0", "1200.00"), ("NT", "0")):
        result = await _run(
            _ctx(gross="3000.00", uk={"tax_code": code, "period_number": 1})
        )
        paye = _component_amounts(result, PayrollComponentRole.WITHHOLDING_LIABILITY)[0]
        assert paye == Decimal(expected), code


async def test_paye_0t_no_allowance() -> None:
    result = await _run(
        _ctx(gross="3000.00", uk={"tax_code": "0T", "period_number": 1})
    )
    paye = _component_amounts(result, PayrollComponentRole.WITHHOLDING_LIABILITY)[0]
    assert paye == Decimal("600.00")   # 3,000 fully taxable @ 20% band


# ---------------------------------------------------------------------------
# NI — category A band boundaries (weekly) + category C.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gross", "employee", "employer"),
    [
        ("90.00", "0.00", "0.00"),         # below ST and PT
        ("96.00", "0.00", "0.00"),         # exactly ST — employer starts ABOVE it
        ("242.00", "0.00", "21.90"),       # exactly PT — employee starts above
        ("967.00", "58.00", "130.65"),     # exactly UEL — full main band
        ("1200.00", "62.66", "165.60"),    # above UEL — 2% additional kicks in
    ],
)
async def test_ni_category_a_weekly_boundaries(gross, employee, employer) -> None:
    result = await _run(
        _ctx(gross=gross, period="WEEKLY", uk={"tax_code": "NT", "period_number": 1})
    )
    withholding = _component_amounts(result, PayrollComponentRole.WITHHOLDING_LIABILITY)
    assert withholding[1] == Decimal(employee)   # [0] is PAYE (0 under NT)
    assert _component_amounts(result, PayrollComponentRole.EMPLOYER_SOCIAL_LIABILITY) == [Decimal(employer)]


async def test_ni_category_c_no_employee_contribution() -> None:
    result = await _run(
        _ctx(
            gross="3000.00",
            uk={"tax_code": "1257L", "period_number": 1, "ni_category": "C"},
        )
    )
    withholding = _component_amounts(result, PayrollComponentRole.WITHHOLDING_LIABILITY)
    assert withholding[1] == Decimal("0")
    assert _component_amounts(result, PayrollComponentRole.EMPLOYER_SOCIAL_LIABILITY) == [Decimal("387.45")]


# ---------------------------------------------------------------------------
# Employment Allowance.
# ---------------------------------------------------------------------------


def test_employment_allowance_amount_and_exclusion_rule() -> None:
    assert EMPLOYMENT_ALLOWANCE_ANNUAL == Decimal("10500.00")
    assert employment_allowance_eligible(sole_employee_above_st_is_director=False)
    assert not employment_allowance_eligible(sole_employee_above_st_is_director=True)
    assert not employment_allowance_eligible(
        sole_employee_above_st_is_director=False, public_body=True
    )


# ---------------------------------------------------------------------------
# Student loans + pension.
# ---------------------------------------------------------------------------


async def test_student_loan_plan2_floor_pound() -> None:
    result = await _run(
        _ctx(
            gross="3000.00",
            uk={"tax_code": "NT", "period_number": 1, "student_loan_plan": "PLAN_2"},
        )
    )
    withholding = _component_amounts(result, PayrollComponentRole.WITHHOLDING_LIABILITY)
    # (3,000 - 2,448.75) x 9% = 49.6125 -> floor to whole pound = 49.
    assert withholding[2] == Decimal("49")


async def test_postgraduate_loan_stacks_with_plan() -> None:
    result = await _run(
        _ctx(
            gross="3000.00",
            uk={
                "tax_code": "NT",
                "period_number": 1,
                "student_loan_plan": "PLAN_2",
                "postgraduate_loan": True,
            },
        )
    )
    withholding = _component_amounts(result, PayrollComponentRole.WITHHOLDING_LIABILITY)
    assert withholding[2] == Decimal("49")
    assert withholding[3] == Decimal("75")   # (3,000 - 1,750) x 6% = 75


async def test_auto_enrolment_pension_qualifying_band() -> None:
    result = await _run(
        _ctx(
            gross="3000.00",
            uk={
                "tax_code": "1257L",
                "period_number": 1,
                "pension": {"employee_percent": 5, "employer_percent": 3},
            },
        )
    )
    # Qualifying earnings: 3,000 - 520 = 2,480 -> ee 124.00, er 74.40.
    assert _component_amounts(result, PayrollComponentRole.RETIREMENT_LIABILITY) == [Decimal("198.40")]
    assert _component_amounts(result, PayrollComponentRole.RETIREMENT_EXPENSE) == [Decimal("74.40")]
    assert result.net == Decimal("2329.64")   # 3,000 - 390.20 - 156.16 - 124.00


# ---------------------------------------------------------------------------
# Hard refusals — never a silent wrong number.
# ---------------------------------------------------------------------------


async def _assert_refuses(ctx: PayrollContext, match: str) -> None:
    with pytest.raises(UKPayrollUnsupported, match=match):
        await _run(ctx)


async def test_refuses_missing_uk_block() -> None:
    await _assert_refuses(_ctx(gross="3000.00"), "extra\\['uk'\\]")


async def test_refuses_k_code() -> None:
    await _assert_refuses(
        _ctx(gross="3000.00", uk={"tax_code": "K475", "period_number": 1}),
        "K code",
    )


async def test_refuses_scottish_flat_code() -> None:
    await _assert_refuses(
        _ctx(gross="3000.00", uk={"tax_code": "SD0", "period_number": 1}),
        "flat-rate code",
    )


async def test_refuses_week_53() -> None:
    await _assert_refuses(
        _ctx(gross="500.00", period="WEEKLY", uk={"tax_code": "1257L", "period_number": 53}),
        "53/54/56",
    )


async def test_refuses_payrolled_benefits() -> None:
    await _assert_refuses(
        _ctx(gross="3000.00", uk={"tax_code": "1257L", "period_number": 1, "payrolled_benefits": True}),
        "benefits in kind",
    )


async def test_refuses_director_ni() -> None:
    await _assert_refuses(
        _ctx(gross="3000.00", uk={"tax_code": "1257L", "period_number": 1, "director": True}),
        "director NI",
    )


async def test_refuses_unverified_ni_category() -> None:
    await _assert_refuses(
        _ctx(gross="3000.00", uk={"tax_code": "1257L", "period_number": 1, "ni_category": "M"}),
        "category 'M'",
    )


async def test_refuses_net_pay_pension_arrangement() -> None:
    await _assert_refuses(
        _ctx(
            gross="3000.00",
            uk={
                "tax_code": "1257L",
                "period_number": 1,
                "pension": {"employee_percent": 5, "employer_percent": 3, "arrangement": "net_pay"},
            },
        ),
        "net-pay",
    )


async def test_refuses_cumulative_refund() -> None:
    await _assert_refuses(
        _ctx(
            gross="100.00",
            uk={
                "tax_code": "1257L",
                "period_number": 2,
                "ytd_taxable_pay": "5000.00",
                "ytd_tax_paid": "2000.00",
            },
        ),
        "refund",
    )


async def test_refuses_statutory_payment_runs() -> None:
    await _assert_refuses(
        _ctx(gross="500.00", uk={"tax_code": "1257L", "period_number": 1, "statutory_payment": "SMP"}),
        "statutory payments",
    )


async def test_refuses_missing_period_number() -> None:
    await _assert_refuses(
        _ctx(gross="3000.00", uk={"tax_code": "1257L"}),
        "period_number",
    )
