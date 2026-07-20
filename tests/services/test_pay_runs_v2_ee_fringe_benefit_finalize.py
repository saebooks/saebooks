"""EE pay-run finalize GL posting — fringe-benefit tax legs (kmd-inf-tsd
follow-up, Packet 2c).

Extends Packet 1's ``_finalize_ee`` coverage
(``test_pay_runs_v2_ee_finalize.py``, reused here via import — same
cross-module pattern ``test_tsd_generator.py`` already established for
cross-packet fixture reuse) with the company-car fringe-benefit legs
Packet 2 adds: proves the two Dr-expense/Cr-payable pairs post, the
underlying wage JE and net-pay clearing are UNTOUCHED by the benefit
(module docstring's "no net-pay effect" requirement), the journal still
balances, and a company with a fringe-benefit line but no fringe-
benefit account settings gets the same loud refusal as every other
settings-key gap in this file.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.journal import JournalLine
from saebooks.models.pay_run import PayRunStatus
from saebooks.services.pay_runs_v2 import (
    FringeBenefitLine,
    PayLineInput,
    PayRunV2Error,
    finalize_with_je,
    upsert_line,
)
from tests.services.test_pay_runs_v2_ee import _make_employee, _make_pay_run
from tests.services.test_pay_runs_v2_ee_finalize import _seed_ee_payroll_accounts
from tests.services.test_tax_return_generator import _make_ee_company

pytestmark = pytest.mark.postgres_only

_FB_INCOME_TAX_EXP = "6-3140"
_FB_SOCIAL_TAX_EXP = "6-3150"
_FB_INCOME_TAX_LIAB = "2-2170"
_FB_SOCIAL_TAX_LIAB = "2-2180"


async def _seed_fringe_benefit_accounts(company_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        accounts = [
            Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code=_FB_INCOME_TAX_EXP, name="EE Fringe benefit income tax expense", account_type=AccountType.EXPENSE),
            Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code=_FB_SOCIAL_TAX_EXP, name="EE Fringe benefit social tax expense", account_type=AccountType.EXPENSE),
            Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code=_FB_INCOME_TAX_LIAB, name="EE Fringe benefit income tax payable", account_type=AccountType.LIABILITY),
            Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code=_FB_SOCIAL_TAX_LIAB, name="EE Fringe benefit social tax payable", account_type=AccountType.LIABILITY),
        ]
        for acct in accounts:
            session.add(acct)
        await session.commit()

        # Fixer round 4 (F1): per-company override columns on
        # ``companies`` (0200), not a global ``Setting`` row.
        company = await session.get(Company, company_id)
        company.ee_payroll_fringe_benefit_income_tax_expense_account_code = _FB_INCOME_TAX_EXP
        company.ee_payroll_fringe_benefit_social_tax_expense_account_code = _FB_SOCIAL_TAX_EXP
        company.ee_payroll_fringe_benefit_income_tax_payable_account_code = _FB_INCOME_TAX_LIAB
        company.ee_payroll_fringe_benefit_social_tax_payable_account_code = _FB_SOCIAL_TAX_LIAB
        await session.commit()


async def _lines_for_entry(entry_id: uuid.UUID) -> list[JournalLine]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(JournalLine).where(JournalLine.entry_id == entry_id)
        )
        return list(result.scalars().all())


async def _by_code(company_id: uuid.UUID) -> dict[str, uuid.UUID]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Account.code, Account.id).where(Account.company_id == company_id)
        )
        return {code: aid for code, aid in result.all()}


async def test_company_car_fringe_benefit_posts_two_tax_legs_no_net_pay_effect() -> None:
    """E1 gross 500 (Packet 3 golden: income_tax=0, unemployment_employee
    =8.00, social_tax=292.38 floor, pillar_ii=10.00, net=482.00) PLUS a
    110kW/2-year-old company car (Packet 2 golden: taxable value 215.60,
    income tax 60.81, social tax 91.22). The wage-side JE legs and net
    pay are BYTE-IDENTICAL to the no-fringe-benefit case — proving the
    benefit's two tax legs are additive, not folded into net."""
    company_id = await _make_ee_company(jurisdiction="EE")
    await _seed_ee_payroll_accounts(company_id)
    await _seed_fringe_benefit_accounts(company_id)

    e1 = await _make_employee(company_id, name="E1 Car Driver", base_rate=Decimal("500.00"))
    pay_run = await _make_pay_run(company_id)
    async with AsyncSessionLocal() as session:
        await upsert_line(
            session, pay_run_id=pay_run.id,
            line_input=PayLineInput(
                employee_id=e1.id, ordinary_hours=Decimal("1"), overtime_hours=Decimal("0"),
                fringe_benefits=(
                    FringeBenefitLine(
                        benefit_category="motor_vehicle",
                        engine_power_kw=Decimal("110"), car_age_years=2,
                    ),
                ),
            ),
            tenant_id=DEFAULT_TENANT_ID, actor="test",
        )

    async with AsyncSessionLocal() as session:
        finalized = await finalize_with_je(
            session, pay_run.id, tenant_id=DEFAULT_TENANT_ID, actor="test",
        )
    assert finalized.status == PayRunStatus.FINALIZED

    by_code = await _by_code(company_id)
    lines = await _lines_for_entry(finalized.journal_id)
    totals: dict[str, tuple[Decimal, Decimal]] = {}
    for ln in lines:
        code = next(c for c, aid in by_code.items() if aid == ln.account_id)
        d, c = totals.get(code, (Decimal("0"), Decimal("0")))
        totals[code] = (d + ln.debit, c + ln.credit)

    # Wage-side legs — same as the no-fringe-benefit E1 golden.
    assert totals["6-3110"] == (Decimal("500.00"), Decimal("0"))       # wages
    assert totals["6-3120"] == (Decimal("292.38"), Decimal("0"))       # social tax (wage)
    assert totals["6-3130"] == (Decimal("4.00"), Decimal("0"))         # unemployment employer
    assert totals["2-2120"] == (Decimal("0"), Decimal("8.00"))         # unemployment employee
    assert totals["2-2130"] == (Decimal("0"), Decimal("10.00"))        # pillar II
    assert totals["2-2140"] == (Decimal("0"), Decimal("292.38"))       # social tax payable (wage)
    assert totals["2-2150"] == (Decimal("0"), Decimal("4.00"))         # unemployment employer payable
    # Net pay UNCHANGED by the fringe benefit — the "no net-pay effect"
    # requirement, proven numerically, not just by code inspection.
    assert totals["2-2160"] == (Decimal("0"), Decimal("482.00"))
    # E1's income tax is 0 (basic exemption swallows it) — no leg posted.
    assert "2-2110" not in totals

    # Fringe-benefit legs — additive, separate accounts.
    assert totals[_FB_INCOME_TAX_EXP] == (Decimal("60.81"), Decimal("0"))
    assert totals[_FB_SOCIAL_TAX_EXP] == (Decimal("91.22"), Decimal("0"))
    assert totals[_FB_INCOME_TAX_LIAB] == (Decimal("0"), Decimal("60.81"))
    assert totals[_FB_SOCIAL_TAX_LIAB] == (Decimal("0"), Decimal("91.22"))

    total_debit = sum((d for d, _ in totals.values()), Decimal("0"))
    total_credit = sum((c for _, c in totals.values()), Decimal("0"))
    assert total_debit == total_credit  # journal_svc.post's own balance check, belt-and-braces


async def test_fringe_benefit_missing_account_setting_refused() -> None:
    """A fringe-benefit line with none of the 4 new settings configured
    is refused loudly — same posture as every other settings-key gap in
    this packet's Packet-1 sibling test file."""
    company_id = await _make_ee_company(jurisdiction="EE")
    await _seed_ee_payroll_accounts(company_id)
    # Deliberately do NOT seed fringe-benefit accounts/settings.

    e1 = await _make_employee(company_id, name="E1 Car Driver", base_rate=Decimal("500.00"))
    pay_run = await _make_pay_run(company_id)
    async with AsyncSessionLocal() as session:
        await upsert_line(
            session, pay_run_id=pay_run.id,
            line_input=PayLineInput(
                employee_id=e1.id, ordinary_hours=Decimal("1"), overtime_hours=Decimal("0"),
                fringe_benefits=(
                    FringeBenefitLine(
                        benefit_category="motor_vehicle",
                        engine_power_kw=Decimal("110"), car_age_years=2,
                    ),
                ),
            ),
            tenant_id=DEFAULT_TENANT_ID, actor="test",
        )

    async with AsyncSessionLocal() as session:
        with pytest.raises(
            PayRunV2Error, match="ee_payroll_fringe_benefit_income_tax_expense_account_code"
        ):
            await finalize_with_je(
                session, pay_run.id, tenant_id=DEFAULT_TENANT_ID, actor="test",
            )


async def test_no_fringe_benefit_line_never_touches_new_settings() -> None:
    """A plain wage-only pay run (no fringe_benefits input) finalizes
    exactly as before this packet — the 4 new settings are never
    consulted, so an EE company that has not configured them (every
    pre-Packet-2 company) is unaffected. Regression guard for the
    ``has_fringe_benefits`` lazy-resolution gate in ``_finalize_ee``."""
    company_id = await _make_ee_company(jurisdiction="EE")
    await _seed_ee_payroll_accounts(company_id)
    # Deliberately do NOT seed fringe-benefit accounts/settings.

    e1 = await _make_employee(company_id, name="E1 No Car", base_rate=Decimal("500.00"))
    pay_run = await _make_pay_run(company_id)
    async with AsyncSessionLocal() as session:
        await upsert_line(
            session, pay_run_id=pay_run.id,
            line_input=PayLineInput(
                employee_id=e1.id, ordinary_hours=Decimal("1"), overtime_hours=Decimal("0"),
            ),
            tenant_id=DEFAULT_TENANT_ID, actor="test",
        )

    async with AsyncSessionLocal() as session:
        finalized = await finalize_with_je(
            session, pay_run.id, tenant_id=DEFAULT_TENANT_ID, actor="test",
        )
    assert finalized.status == PayRunStatus.FINALIZED
