"""LT jurisdiction-module registration — the bolt-on contract holds.

Pins that registering LT touched nothing it shouldn't: LT resolves to
its own engines through every per-capability registry, the descriptor
catalogue lists it, and the AU/EE/neutral registrations are unchanged.

Deliberately MEMBERSHIP-style assertions (not exact descriptor lists,
and no "X is still a stub" pins on OTHER modules) — the exact-list
shape is what turned test_jurisdiction_uk_registration.py red the
moment the NZ module merged beside it; a concurrent LV build merges
beside this one.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from saebooks.services import jurisdiction_modules as jm
from saebooks.services.payroll import (
    NeutralPayrollEngine,
    PayrollComponentRole,
    get_payroll_engine,
    get_posting_profile,
)
from saebooks.services.tax_engine import get_engine, resolve_engine


def test_lt_descriptor_registered() -> None:
    d = jm.get_descriptor("LT")
    assert d is not None
    assert d.label == "Lithuania"
    assert d.provides_tax and d.provides_payroll and d.provides_lodgement
    assert d.has_seed_dir
    codes = [x.code for x in jm.list_descriptors()]
    assert "LT" in codes
    assert codes == sorted(codes)


def test_lt_tax_engine_dispatches() -> None:
    engine = get_engine("LT")
    assert type(engine).__name__ == "LTTaxEngine"
    assert engine.jurisdiction == "LT"
    # The posting path's never-raise resolver reaches the same engine.
    assert type(resolve_engine("LT")).__name__ == "LTTaxEngine"


def test_lt_payroll_engine_and_posting_profile() -> None:
    engine = get_payroll_engine("LT")
    assert type(engine).__name__ == "LTPayrollEngine"
    profile = get_posting_profile("LT")
    assert profile.wages_account_code == "6301"
    assert profile.net_account_code == "4462"
    by_role: dict[PayrollComponentRole, list[str]] = {}
    for ra in profile.role_accounts:
        by_role.setdefault(ra.role, []).append(ra.account_code)
    assert by_role[PayrollComponentRole.WITHHOLDING_LIABILITY] == ["4460"]
    assert by_role[PayrollComponentRole.EMPLOYER_SOCIAL_EXPENSE] == ["6302"]
    assert by_role[PayrollComponentRole.EMPLOYER_SOCIAL_LIABILITY] == ["4463"]
    # No retirement mapping — the II pillar is employee-funded and
    # rides WITHHOLDING (see jurisdictions/lt/payroll.py role mapping).
    assert PayrollComponentRole.RETIREMENT_LIABILITY not in by_role


def test_lt_lodgement_adapter_dispatches() -> None:
    from saebooks.services.lodgement.registry import get_adapter

    adapter = get_adapter("LT")
    assert type(adapter).__name__ == "LTLodgementAdapter"
    assert adapter.jurisdiction == "LT"


def test_other_registrations_untouched() -> None:
    assert type(get_engine("AU")).__name__ == "AUTaxEngine"
    assert type(get_engine("EE")).__name__ == "EETaxEngine"
    assert type(get_payroll_engine("AU")).__name__ == "AUPayrollEngine"
    # The neutral sentinel still degrades to the null objects.
    assert isinstance(get_payroll_engine("XX"), NeutralPayrollEngine)
    neutral_profile = get_posting_profile("XX")
    assert neutral_profile.role_accounts == ()
    # And a genuinely unregistered code still degrades, never raises.
    assert isinstance(get_payroll_engine("ZZ"), NeutralPayrollEngine)


async def test_lt_payroll_line_balances_through_the_registry() -> None:
    """The invariant finalize_with_je's role-tag posting relies on,
    reached through the REGISTRY (not a direct engine instantiation):
    gross + expense components == liability components + net."""
    ctx_extra = {
        "lt": {
            "apply_npd": True,
            "pillar_ii": True,
        }
    }
    from saebooks.services.payroll.types import PayrollContext

    ctx = PayrollContext(
        company_id=uuid.uuid4(),
        employee_id=uuid.uuid4(),
        pay_run_id=uuid.uuid4(),
        period="MONTHLY",
        period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31),
        effective_date=date(2026, 5, 31),
        gross=Decimal("3000.00"),
        ote=Decimal("3000.00"),
        deductions_total=Decimal("0"),
        extra=ctx_extra,
    )
    result = await get_payroll_engine("LT").compute_line(None, ctx)
    debits = result.gross + sum(
        (c.amount for c in result.components if c.role.posts_debit),
        start=Decimal("0"),
    )
    credits = result.net + sum(
        (c.amount for c in result.components if not c.role.posts_debit),
        start=Decimal("0"),
    )
    assert debits == credits, f"pay-run JE would not balance: {debits} != {credits}"
