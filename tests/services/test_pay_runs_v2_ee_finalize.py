"""EE pay-run finalize GL posting — Packet 1 (kmd-inf-tsd follow-up).

``finalize_with_je`` used to hard-refuse any non-AU jurisdiction (see
its own dispatch comment, pre-Packet-1). This proves the real
``_finalize_ee`` branch: a balanced journal posts on finalize, every
component reconciles against the SAME golden month
``test_pay_runs_v2_ee.py``'s ``test_ee_golden_month_e1_and_e2``
established (E1 gross 500 / E2 gross 2000 — there is no "gross 2200"
scenario anywhere in this tree, see this packet's build report),
``void_pay_run`` reverses it back to net-zero, and the pre-existing AU
branch is unperturbed (its own dedicated regression test below, since
``finalize_with_je`` had NO test coverage at all before this packet).
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
from saebooks.models.employee import Employee
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine
from saebooks.models.pay_run import PayRun, PayRunLine, PayRunStatus
from saebooks.services.pay_runs_v2 import (
    PayLineInput,
    PayRunV2Error,
    finalize_with_je,
    upsert_line,
    void_pay_run,
)
from tests.services.test_pay_runs_v2_ee import _make_employee, _make_pay_run
from tests.services.test_tax_return_generator import _make_ee_company

pytestmark = pytest.mark.postgres_only

# EE payroll GL account codes used by this test's fixture company — must
# match whatever codes the settings below point the resolver at.
_EE_WAGES = "6-3110"
_EE_SOCIAL_TAX_EXP = "6-3120"
_EE_UNEMP_ER_EXP = "6-3130"
_EE_INCOME_TAX_LIAB = "2-2110"
_EE_UNEMP_EE_LIAB = "2-2120"
_EE_PILLAR_II_LIAB = "2-2130"
_EE_SOCIAL_TAX_LIAB = "2-2140"
_EE_UNEMP_ER_LIAB = "2-2150"
_EE_NET_CLEARING = "2-2160"

# These must be the EXACT literal codes the AU payroll posting profile
# books to (``jurisdictions.au.PAYROLL_POSTING`` — formerly
# ``pay_runs_v2``'s hardcoded ``_ACCT_*`` constants; AU is NOT
# settings-driven, unlike the EE keys above), so the regression
# company below is built
# from scratch rather than reusing ``_make_ee_company`` (which seeds an
# account at "2-1310" for GST Collected unconditionally, regardless of
# jurisdiction — a straight collision with AU's PAYG-liability code).
_AU_WAGES = "6-2110"
_AU_SUPER_EXP = "6-2120"
_AU_PAYG_LIAB = "2-1310"
_AU_SUPER_LIAB = "2-1320"
_AU_NET_CLEARING = "2-1150"


async def _seed_ee_payroll_accounts(company_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        accounts = [
            Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code=_EE_WAGES, name="EE Wages expense", account_type=AccountType.EXPENSE),
            Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code=_EE_SOCIAL_TAX_EXP, name="EE Social tax expense", account_type=AccountType.EXPENSE),
            Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code=_EE_UNEMP_ER_EXP, name="EE Unemployment (employer) expense", account_type=AccountType.EXPENSE),
            Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code=_EE_INCOME_TAX_LIAB, name="EE Income tax payable", account_type=AccountType.LIABILITY),
            Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code=_EE_UNEMP_EE_LIAB, name="EE Unemployment (employee) payable", account_type=AccountType.LIABILITY),
            Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code=_EE_PILLAR_II_LIAB, name="EE Pillar II payable", account_type=AccountType.LIABILITY),
            Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code=_EE_SOCIAL_TAX_LIAB, name="EE Social tax payable", account_type=AccountType.LIABILITY),
            Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code=_EE_UNEMP_ER_LIAB, name="EE Unemployment (employer) payable", account_type=AccountType.LIABILITY),
            Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code=_EE_NET_CLEARING, name="EE Net wages payable", account_type=AccountType.LIABILITY),
        ]
        for acct in accounts:
            session.add(acct)
        await session.commit()

        # Fixer round 4 (F1): these are now per-company override columns
        # on ``companies`` (0200), not a global ``Setting`` row — set
        # them directly on this test's own Company row so two EE
        # companies in the same test session can't collide.
        company = await session.get(Company, company_id)
        company.ee_payroll_wages_expense_account_code = _EE_WAGES
        company.ee_payroll_social_tax_expense_account_code = _EE_SOCIAL_TAX_EXP
        company.ee_payroll_unemployment_employer_expense_account_code = _EE_UNEMP_ER_EXP
        company.ee_payroll_income_tax_payable_account_code = _EE_INCOME_TAX_LIAB
        company.ee_payroll_unemployment_employee_payable_account_code = _EE_UNEMP_EE_LIAB
        company.ee_payroll_pillar_ii_payable_account_code = _EE_PILLAR_II_LIAB
        company.ee_payroll_social_tax_payable_account_code = _EE_SOCIAL_TAX_LIAB
        company.ee_payroll_unemployment_employer_payable_account_code = _EE_UNEMP_ER_LIAB
        company.ee_payroll_net_pay_clearing_account_code = _EE_NET_CLEARING
        await session.commit()


async def _make_au_payroll_company() -> uuid.UUID:
    """A from-scratch AU company (not ``_make_ee_company`` — see the
    ``_AU_*`` code comment above for why)."""
    company_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Company(
                id=company_id, tenant_id=DEFAULT_TENANT_ID,
                name=f"AU Payroll Regression {company_id.hex[:8]}",
                base_currency="AUD", fin_year_start_month=7,
                audit_mode="immutable", jurisdiction="AU",
            )
        )
        await session.commit()
    return company_id


async def _seed_au_payroll_accounts(company_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        accounts = [
            Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code=_AU_WAGES, name="Wages — gross", account_type=AccountType.EXPENSE),
            Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code=_AU_SUPER_EXP, name="Superannuation expense", account_type=AccountType.EXPENSE),
            Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code=_AU_PAYG_LIAB, name="PAYG withholding payable", account_type=AccountType.LIABILITY),
            Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code=_AU_SUPER_LIAB, name="Superannuation payable", account_type=AccountType.LIABILITY),
            Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code=_AU_NET_CLEARING, name="Payments — pending", account_type=AccountType.LIABILITY),
        ]
        for acct in accounts:
            session.add(acct)
        await session.commit()


async def _make_golden_month(company_id: uuid.UUID) -> tuple[PayRun, Employee, Employee]:
    e1 = await _make_employee(company_id, name="E1 Low Wage", base_rate=Decimal("500.00"))
    e2 = await _make_employee(
        company_id, name="E2 Pillar Elect",
        base_rate=Decimal("2000.00"), pillar_ii_rate_percent=Decimal("6.0"),
    )
    pay_run = await _make_pay_run(company_id)
    async with AsyncSessionLocal() as session:
        await upsert_line(
            session, pay_run_id=pay_run.id,
            line_input=PayLineInput(employee_id=e1.id, ordinary_hours=Decimal("1"), overtime_hours=Decimal("0")),
            tenant_id=DEFAULT_TENANT_ID, actor="test",
        )
    async with AsyncSessionLocal() as session:
        await upsert_line(
            session, pay_run_id=pay_run.id,
            line_input=PayLineInput(employee_id=e2.id, ordinary_hours=Decimal("1"), overtime_hours=Decimal("0")),
            tenant_id=DEFAULT_TENANT_ID, actor="test",
        )
    return pay_run, e1, e2


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


async def test_ee_finalize_posts_balanced_journal_golden_month() -> None:
    """Golden month (E1 gross 500 / E2 gross 2000 — the ACTUAL Packet-3
    figures, see module docstring). Every component asserted per
    employee against ``services.payroll_ee``'s own golden numbers
    (``test_ee_golden_month_e1_and_e2``): E1 income_tax=0 (skipped, no
    zero-amount line), unemployment_employee=8.00, social_tax=292.38
    (886 floor), pillar_ii=10.00; E2 income_tax=252.56,
    unemployment_employee=32.00, social_tax=660.00, pillar_ii=120.00.
    Employer legs: unemployment_employer E1=4.00/E2=16.00."""
    company_id = await _make_ee_company(jurisdiction="EE")
    await _seed_ee_payroll_accounts(company_id)
    pay_run, e1, e2 = await _make_golden_month(company_id)

    async with AsyncSessionLocal() as session:
        finalized = await finalize_with_je(
            session, pay_run.id, tenant_id=DEFAULT_TENANT_ID, actor="test",
        )
    assert finalized.status == PayRunStatus.FINALIZED
    assert finalized.journal_id is not None

    by_code = await _by_code(company_id)
    lines = await _lines_for_entry(finalized.journal_id)
    # sum debit/credit per account code across BOTH employees
    totals: dict[str, tuple[Decimal, Decimal]] = {}
    for ln in lines:
        code = next(c for c, aid in by_code.items() if aid == ln.account_id)
        d, c = totals.get(code, (Decimal("0"), Decimal("0")))
        totals[code] = (d + ln.debit, c + ln.credit)

    # Dr legs
    assert totals[_EE_WAGES] == (Decimal("2500.00"), Decimal("0"))       # 500 + 2000
    assert totals[_EE_SOCIAL_TAX_EXP] == (Decimal("952.38"), Decimal("0"))  # 292.38 + 660.00
    assert totals[_EE_UNEMP_ER_EXP] == (Decimal("20.00"), Decimal("0"))     # 4.00 + 16.00
    # Cr legs
    assert totals[_EE_INCOME_TAX_LIAB] == (Decimal("0"), Decimal("252.56"))  # E1=0 skipped, E2=252.56
    assert totals[_EE_UNEMP_EE_LIAB] == (Decimal("0"), Decimal("40.00"))     # 8.00 + 32.00
    assert totals[_EE_PILLAR_II_LIAB] == (Decimal("0"), Decimal("130.00"))   # 10.00 + 120.00
    assert totals[_EE_SOCIAL_TAX_LIAB] == (Decimal("0"), Decimal("952.38"))
    assert totals[_EE_UNEMP_ER_LIAB] == (Decimal("0"), Decimal("20.00"))
    assert totals[_EE_NET_CLEARING] == (Decimal("0"), Decimal("2077.44"))  # 482.00 + 1595.44

    total_debit = sum((d for d, _ in totals.values()), Decimal("0"))
    total_credit = sum((c for _, c in totals.values()), Decimal("0"))
    assert total_debit == total_credit == Decimal("3472.38")


async def test_ee_finalize_void_nets_to_zero() -> None:
    """``void_pay_run`` reverses the journal; the pay run flips to
    VOIDED. Net-zero must be asserted over BOTH the REVERSED original
    and its POSTED reversal (a POSTED-only view sees only the reversal
    and reads as the negative of the original, not zero — same trap
    ``_kmd_box_vector``'s REPORTABLE_STATUSES convention exists for)."""
    company_id = await _make_ee_company(jurisdiction="EE")
    await _seed_ee_payroll_accounts(company_id)
    pay_run, _e1, _e2 = await _make_golden_month(company_id)

    async with AsyncSessionLocal() as session:
        finalized = await finalize_with_je(
            session, pay_run.id, tenant_id=DEFAULT_TENANT_ID, actor="test",
        )
    original_journal_id = finalized.journal_id
    assert original_journal_id is not None

    async with AsyncSessionLocal() as session:
        voided = await void_pay_run(
            session, pay_run.id, tenant_id=DEFAULT_TENANT_ID, actor="test",
        )
    assert voided.status == PayRunStatus.VOIDED

    async with AsyncSessionLocal() as session:
        original = await session.get(JournalEntry, original_journal_id)
        assert original.status == EntryStatus.REVERSED
        reversal_result = await session.execute(
            select(JournalEntry).where(JournalEntry.reversal_of_id == original_journal_id)
        )
        reversal = reversal_result.scalars().one()
        assert reversal.status == EntryStatus.POSTED

    original_lines = await _lines_for_entry(original_journal_id)
    reversal_lines = await _lines_for_entry(reversal.id)

    # Sum (debit - credit) per account across BOTH entries — must net to
    # zero for every account touched.
    net: dict[uuid.UUID, Decimal] = {}
    for ln in (*original_lines, *reversal_lines):
        net[ln.account_id] = net.get(ln.account_id, Decimal("0")) + ln.debit - ln.credit
    assert all(v == Decimal("0") for v in net.values()), net

    # Voiding again is a no-op (idempotent), not an error.
    async with AsyncSessionLocal() as session:
        voided_again = await void_pay_run(
            session, pay_run.id, tenant_id=DEFAULT_TENANT_ID, actor="test",
        )
    assert voided_again.status == PayRunStatus.VOIDED


async def test_ee_finalize_missing_account_setting_refused() -> None:
    """A blank/unresolved control-account column is a loud config error,
    never a silently-wrong or unbalanced posting — mirrors
    ``services.bills``'s RC-payable-account precedent. Deliberately do
    NOT seed payroll accounts for this company — the column is NULL by
    default (0200), same as never having been configured."""
    company_id = await _make_ee_company(jurisdiction="EE")
    pay_run, _e1, _e2 = await _make_golden_month(company_id)

    async with AsyncSessionLocal() as session:
        with pytest.raises(PayRunV2Error, match="ee_payroll_wages_expense_account_code"):
            await finalize_with_je(
                session, pay_run.id, tenant_id=DEFAULT_TENANT_ID, actor="test",
            )


async def test_au_pay_run_finalize_regression_untouched() -> None:
    """The pre-Packet-1 AU JE shape is unperturbed: 5-leg
    Dr-Wages/Dr-Super/Cr-PAYG/Cr-Super/Cr-Net, same accounts, same
    dispatch. ``finalize_with_je`` had NO test coverage before this
    packet — this is the regression test the packet's own test plan
    calls for."""
    company_id = await _make_au_payroll_company()
    await _seed_au_payroll_accounts(company_id)
    emp = await _make_employee(company_id, name="AU Employee", base_rate=Decimal("40.00"))
    pay_run = await _make_pay_run(company_id)

    async with AsyncSessionLocal() as session:
        await upsert_line(
            session, pay_run_id=pay_run.id,
            line_input=PayLineInput(employee_id=emp.id, ordinary_hours=Decimal("152"), overtime_hours=Decimal("0")),
            tenant_id=DEFAULT_TENANT_ID, actor="test",
        )

    async with AsyncSessionLocal() as session:
        finalized = await finalize_with_je(
            session, pay_run.id, tenant_id=DEFAULT_TENANT_ID, actor="test",
        )
    assert finalized.status == PayRunStatus.FINALIZED
    assert finalized.journal_id is not None

    by_code = await _by_code(company_id)
    lines = await _lines_for_entry(finalized.journal_id)
    # No EE-only accounts should exist at all in this company's chart,
    # and every line must resolve to one of the 5 AU codes.
    au_codes = {_AU_WAGES, _AU_SUPER_EXP, _AU_PAYG_LIAB, _AU_SUPER_LIAB, _AU_NET_CLEARING}
    for ln in lines:
        code = next(c for c, aid in by_code.items() if aid == ln.account_id)
        assert code in au_codes

    # Balanced (journal_svc.post's own _check_balance already enforces
    # this at post time — belt-and-braces here) and wages leg = gross,
    # same as pre-Packet-1 (exact PAYG/super figures are
    # test_payg.py/test_super_calc.py's concern, not this dispatch
    # test's — mirrors test_au_pay_run_regression_untouched's own
    # posture in test_pay_runs_v2_ee.py).
    total_debit = sum((ln.debit for ln in lines), Decimal("0"))
    total_credit = sum((ln.credit for ln in lines), Decimal("0"))
    assert total_debit == total_credit
    wages_debit = sum(
        (ln.debit for ln in lines if ln.account_id == by_code[_AU_WAGES]), Decimal("0")
    )
    assert wages_debit == Decimal("6080.00")  # 152 * 40.00, gross unchanged


async def test_xx_pay_run_finalize_posts_wages_and_net_only() -> None:
    """Jurisdiction-module Phase 1: ``finalize_with_je`` no longer
    hard-refuses jurisdictions beyond AU/EE. A company on the neutral
    sentinel (no payroll module) finalizes via the generic role-tagged
    path with the neutral posting profile — a 2-leg JE per employee,
    Dr wages(gross) / Cr net clearing(net), zero statutory legs (the
    ``NeutralPayrollEngine`` computed no withholding/retirement, so the
    stored line carries none)."""
    company_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Company(
                id=company_id, tenant_id=DEFAULT_TENANT_ID,
                name=f"XX Neutral Payroll {company_id.hex[:8]}",
                base_currency="AUD", fin_year_start_month=7,
                audit_mode="immutable", jurisdiction="XX",
            )
        )
        await session.commit()
    # The neutral profile books to the same core CoA-seed codes the AU
    # chart provides for wages/net; the extra AU statutory accounts this
    # helper seeds are simply never referenced.
    await _seed_au_payroll_accounts(company_id)
    emp = await _make_employee(company_id, name="XX Employee", base_rate=Decimal("40.00"))
    pay_run = await _make_pay_run(company_id)

    async with AsyncSessionLocal() as session:
        await upsert_line(
            session, pay_run_id=pay_run.id,
            line_input=PayLineInput(employee_id=emp.id, ordinary_hours=Decimal("10"), overtime_hours=Decimal("0")),
            tenant_id=DEFAULT_TENANT_ID, actor="test",
        )

    async with AsyncSessionLocal() as session:
        finalized = await finalize_with_je(
            session, pay_run.id, tenant_id=DEFAULT_TENANT_ID, actor="test",
        )
    assert finalized.status == PayRunStatus.FINALIZED
    assert finalized.journal_id is not None

    by_code = await _by_code(company_id)
    lines = await _lines_for_entry(finalized.journal_id)
    assert len(lines) == 2, "neutral finalize must post exactly Dr wages / Cr net"
    wages_line = next(ln for ln in lines if ln.account_id == by_code[_AU_WAGES])
    net_line = next(ln for ln in lines if ln.account_id == by_code[_AU_NET_CLEARING])
    assert wages_line.debit == Decimal("400.00")   # 10h * 40.00, no withholding
    assert net_line.credit == Decimal("400.00")    # net == gross for the null engine
    # No statutory legs at all — the AU-only accounts stay untouched.
    for code in (_AU_SUPER_EXP, _AU_PAYG_LIAB, _AU_SUPER_LIAB):
        assert by_code[code] not in {ln.account_id for ln in lines}
