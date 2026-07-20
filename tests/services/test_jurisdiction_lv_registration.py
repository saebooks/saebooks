"""LV jurisdiction-module registration — the bolt-on contract holds.

Pins that registering LV touched nothing it shouldn't: LV resolves to
its own engines through every per-capability registry, the descriptor
catalogue lists it, and the AU/EE/neutral registrations are unchanged.

Deliberately MEMBERSHIP-based (never an exact descriptor-list pin): a
concurrent sibling-module build (LT) registers into the same catalogue,
and an exact-list assertion here would couple the two modules' merges —
the exact-list pin in test_jurisdiction_uk_registration is already
stale for that reason (pre-existing baseline failure, flagged to the
orchestrator, not fixed here — not this module's file).
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from saebooks.services import jurisdiction_modules as jm
from saebooks.services.payroll import (
    PayrollComponentRole,
    get_payroll_engine,
    get_posting_profile,
)
from saebooks.services.payroll.neutral import NeutralPayrollEngine
from saebooks.services.tax_engine import get_engine, resolve_engine


def test_lv_descriptor_registered() -> None:
    d = jm.get_descriptor("LV")
    assert d is not None
    assert d.label == "Latvia"
    assert d.provides_tax and d.provides_payroll and d.provides_lodgement
    assert d.has_seed_dir
    codes = {x.code for x in jm.list_descriptors()}
    assert {"AU", "NZ", "UK", "LV", "XX"} <= codes


def test_lv_tax_engine_dispatches() -> None:
    engine = get_engine("LV")
    assert type(engine).__name__ == "LVTaxEngine"
    assert engine.jurisdiction == "LV"
    # The posting path's never-raise resolver reaches the same engine.
    assert type(resolve_engine("LV")).__name__ == "LVTaxEngine"


def test_lv_payroll_engine_and_posting_profile() -> None:
    engine = get_payroll_engine("LV")
    assert type(engine).__name__ == "LVPayrollEngine"
    profile = get_posting_profile("LV")
    assert profile.wages_account_code == "6100"
    assert profile.net_account_code == "2400"
    by_role: dict[PayrollComponentRole, list[str]] = {}
    for ra in profile.role_accounts:
        by_role.setdefault(ra.role, []).append(ra.account_code)
    assert by_role[PayrollComponentRole.WITHHOLDING_LIABILITY] == ["2300"]
    assert by_role[PayrollComponentRole.EMPLOYER_SOCIAL_LIABILITY] == ["2310"]
    assert by_role[PayrollComponentRole.EMPLOYER_SOCIAL_EXPENSE] == ["6110"]
    # NO retirement roles — Latvia's pillar II is a state-side redirect
    # inside VSAOI, so a pay run has no retirement leg (the EE "no
    # super" independent-capabilities proof, replayed for LV).
    assert PayrollComponentRole.RETIREMENT_LIABILITY not in by_role
    assert PayrollComponentRole.RETIREMENT_EXPENSE not in by_role


def test_lv_lodgement_adapter_dispatches() -> None:
    from saebooks.services.lodgement.registry import get_adapter

    adapter = get_adapter("LV")
    assert type(adapter).__name__ == "LVLodgementAdapter"
    assert adapter.jurisdiction == "LV"


def test_other_registrations_untouched() -> None:
    assert type(get_engine("AU")).__name__ == "AUTaxEngine"
    assert type(get_engine("EE")).__name__ == "EETaxEngine"
    assert type(get_engine("NZ")).__name__ == "NZTaxEngine"
    assert type(get_engine("UK")).__name__ == "UKTaxEngine"
    assert type(get_payroll_engine("AU")).__name__ == "AUPayrollEngine"
    # The neutral sentinel still degrades to the null objects.
    assert isinstance(get_payroll_engine("XX"), NeutralPayrollEngine)
    assert get_posting_profile("XX").role_accounts == ()


async def test_lv_payroll_line_balances_for_je_posting_via_registry() -> None:
    """The invariant finalize_with_je's role-tag posting relies on,
    reached through the registry dispatch (not direct instantiation)."""
    from saebooks.services.payroll.types import PayrollContext

    ctx = PayrollContext(
        company_id=uuid.uuid4(),
        employee_id=uuid.uuid4(),
        pay_run_id=uuid.uuid4(),
        period="MONTHLY",
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
        effective_date=date(2026, 3, 31),
        gross=Decimal("2000.00"),
        ote=Decimal("2000.00"),
        deductions_total=Decimal("0"),
        extra={"lv": {"tax_book_submitted": True, "dependants": 1}},
    )
    result = await get_payroll_engine("LV").compute_line(None, ctx)
    debits = result.gross + sum(
        (c.amount for c in result.components if c.role.posts_debit),
        start=Decimal("0"),
    )
    credits = result.net + sum(
        (c.amount for c in result.components if not c.role.posts_debit),
        start=Decimal("0"),
    )
    assert debits == credits, f"pay-run JE would not balance: {debits} != {credits}"
