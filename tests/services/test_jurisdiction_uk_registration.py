"""UK jurisdiction-module registration — the bolt-on contract holds.

Pins that registering UK touched nothing it shouldn't: UK resolves to
its own engines through every per-capability registry, the descriptor
catalogue lists it, and the AU/EE/neutral registrations are unchanged.
"""
from __future__ import annotations

from decimal import Decimal

from saebooks.services import jurisdiction_modules as jm
from saebooks.services.payroll import (
    NeutralPayrollEngine,
    PayrollComponentRole,
    get_payroll_engine,
    get_posting_profile,
)
from saebooks.services.tax_engine import get_engine, resolve_engine


def test_uk_descriptor_registered() -> None:
    d = jm.get_descriptor("UK")
    assert d is not None
    assert d.label == "United Kingdom"
    assert d.provides_tax and d.provides_payroll and d.provides_lodgement
    assert d.has_seed_dir
    # Membership + ordering, not an exact list — new jurisdiction
    # modules register beside UK without editing this pin.
    codes = [x.code for x in jm.list_descriptors()]
    assert {"AU", "NZ", "UK", "XX"} <= set(codes)
    assert codes == sorted(codes)


def test_uk_tax_engine_dispatches() -> None:
    engine = get_engine("UK")
    assert type(engine).__name__ == "UKTaxEngine"
    assert engine.jurisdiction == "UK"
    # The posting path's never-raise resolver reaches the same engine.
    assert type(resolve_engine("UK")).__name__ == "UKTaxEngine"


def test_uk_payroll_engine_and_posting_profile() -> None:
    engine = get_payroll_engine("UK")
    assert type(engine).__name__ == "UKPayrollEngine"
    profile = get_posting_profile("UK")
    assert profile.wages_account_code == "7000"
    assert profile.net_account_code == "2220"
    by_role: dict[PayrollComponentRole, list[str]] = {}
    for ra in profile.role_accounts:
        by_role.setdefault(ra.role, []).append(ra.account_code)
    assert by_role[PayrollComponentRole.WITHHOLDING_LIABILITY] == ["2210"]
    assert by_role[PayrollComponentRole.EMPLOYER_SOCIAL_LIABILITY] == ["2210"]
    assert by_role[PayrollComponentRole.EMPLOYER_SOCIAL_EXPENSE] == ["7006"]
    assert by_role[PayrollComponentRole.RETIREMENT_LIABILITY] == ["2230"]
    assert by_role[PayrollComponentRole.RETIREMENT_EXPENSE] == ["7007"]


def test_uk_lodgement_adapter_dispatches() -> None:
    from saebooks.services.lodgement.registry import get_adapter

    adapter = get_adapter("UK")
    assert type(adapter).__name__ == "UKLodgementAdapter"
    assert adapter.jurisdiction == "UK"


def test_other_registrations_untouched() -> None:
    assert type(get_engine("AU")).__name__ == "AUTaxEngine"
    assert type(get_engine("EE")).__name__ == "EETaxEngine"
    assert type(get_payroll_engine("AU")).__name__ == "AUPayrollEngine"
    # The neutral sentinel still degrades to the null objects.
    assert isinstance(get_payroll_engine("XX"), NeutralPayrollEngine)
    neutral_profile = get_posting_profile("XX")
    assert neutral_profile.role_accounts == ()
    # And the NZ module (merged alongside UK) dispatches its own engine.
    assert type(get_engine("NZ")).__name__ == "NZTaxEngine"


async def test_uk_payroll_line_balances_for_je_posting() -> None:
    """The invariant finalize_with_je's role-tag posting relies on:
    gross + expense components == liability components + net."""
    import uuid
    from datetime import date

    from saebooks.services.payroll.types import PayrollContext

    ctx = PayrollContext(
        company_id=uuid.uuid4(),
        employee_id=uuid.uuid4(),
        pay_run_id=uuid.uuid4(),
        period="MONTHLY",
        period_start=date(2026, 4, 6),
        period_end=date(2026, 5, 5),
        effective_date=date(2026, 5, 5),
        gross=Decimal("3000.00"),
        ote=Decimal("3000.00"),
        deductions_total=Decimal("0"),
        extra={
            "uk": {
                "tax_code": "1257L",
                "period_number": 1,
                "student_loan_plan": "PLAN_2",
                "pension": {"employee_percent": 5, "employer_percent": 3},
            }
        },
    )
    result = await get_payroll_engine("UK").compute_line(None, ctx)
    debits = result.gross + sum(
        (c.amount for c in result.components if c.role.posts_debit),
        start=Decimal("0"),
    )
    credits = result.net + sum(
        (c.amount for c in result.components if not c.role.posts_debit),
        start=Decimal("0"),
    )
    assert debits == credits, f"pay-run JE would not balance: {debits} != {credits}"
