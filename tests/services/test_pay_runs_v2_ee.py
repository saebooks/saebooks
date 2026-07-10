"""Integration tests for the EE dispatch branch in
``saebooks.services.pay_runs_v2`` (kmd-inf-tsd scope Packet 3).

Drives ``upsert_line`` end-to-end against a real (throwaway, isolated)
EE-jurisdiction company + employees, then re-reads the persisted
``pay_run_lines`` row to prove the EE columns (migration
``0191_ee_payroll_compute_cols``) actually land on the wire, not just
in the returned dataclass. Companion AU test proves the new
``company_jurisdiction`` dispatch does not perturb the pre-existing AU
branch (scope's "AU byte-identical" requirement + this build's own
"AU pay-run regression untouched" test-plan line).
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.models.contact import Contact, ContactType
from saebooks.models.employee import Employee, EmploymentBasis, PayBasis, PayFrequency
from saebooks.models.pay_run import PayRun, PayRunLine, PayRunStatus
from saebooks.services.pay_runs_v2 import PayLineInput, upsert_line
from tests.services.test_tax_return_generator import _make_ee_company

pytestmark = pytest.mark.postgres_only

_PERIOD_START = date(2026, 4, 1)
_PERIOD_END = date(2026, 4, 30)
_PAYMENT_DATE = date(2026, 4, 30)


async def _make_employee(
    company_id: uuid.UUID,
    *,
    name: str,
    base_rate: Decimal,
    pillar_ii_rate_percent: Decimal | None = None,
    isikukood: str | None = None,
) -> Employee:
    """``isikukood`` (kmd-inf-tsd scope Packet 4) — optional so every
    pre-Packet-4 caller of this helper is unaffected; TSD golden tests
    pass it to exercise the Lisa-1 row key.

    ``employee_number`` is derived deterministically from ``name``
    (kmd-inf-tsd scope Packet 5 fix-forward), NOT a random UUID suffix as
    before: ``tsd.generator.generate_tsd``'s row order is keyed on
    ``employee_number`` (its own "deterministic order" docstring point),
    so a random employee_number made the byte-for-byte TSD golden test
    (``test_tsd_golden.py``) flake ~50% of the time on E1-vs-E2 row
    order — found running this exact packet's golden test twice. Unique
    per ``(company_id, employee_number)`` (the model's own constraint,
    ``employee.py:129``) is preserved as long as callers use distinct
    ``name`` values within one company, true of every caller in this
    file today."""
    from saebooks.services import crypto

    async with AsyncSessionLocal() as session:
        contact = Contact(
            company_id=company_id, tenant_id=DEFAULT_TENANT_ID,
            name=name, contact_type=ContactType.BENEFICIARY,
        )
        session.add(contact)
        await session.flush()
        slug = "".join(ch for ch in name.upper() if ch.isalnum())[:28]
        emp = Employee(
            company_id=company_id, tenant_id=DEFAULT_TENANT_ID,
            contact_id=contact.id,
            employee_number=f"EMP-{slug}",
            start_date=date(2026, 1, 1),
            employment_basis=EmploymentBasis.FULL_TIME.value,
            pay_basis=PayBasis.HOURLY.value,
            pay_frequency=PayFrequency.MONTHLY.value,
            weekly_hours=Decimal("38.00"),
            base_rate=base_rate,
            ee_pillar_ii_rate_percent=pillar_ii_rate_percent,
            # Both golden employees have filed the avaldus electing
            # THIS employer to apply their basic exemption — the
            # module's actual default (unset -> NOT applied) is a
            # payroll_ee.py-level unit test, not this integration test's
            # concern.
            ee_basic_exemption_elected=True,
            isikukood_encrypted=(
                crypto.encrypt_field(isikukood) if isikukood else None
            ),
        )
        session.add(emp)
        await session.commit()
        await session.refresh(emp)
        return emp


async def _make_pay_run(company_id: uuid.UUID) -> PayRun:
    async with AsyncSessionLocal() as session:
        run = PayRun(
            company_id=company_id, tenant_id=DEFAULT_TENANT_ID,
            period_start=_PERIOD_START, period_end=_PERIOD_END,
            payment_date=_PAYMENT_DATE, status=PayRunStatus.DRAFT,
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run


async def _reload_line(pay_run_id: uuid.UUID, employee_id: uuid.UUID) -> PayRunLine:
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(PayRunLine).where(
                    PayRunLine.pay_run_id == pay_run_id,
                    PayRunLine.employee_id == employee_id,
                )
            )
        ).scalars().first()
        assert row is not None
        return row


async def test_ee_golden_month_e1_and_e2() -> None:
    """The scope §6 golden month, driven through the real upsert path."""
    company_id = await _make_ee_company(jurisdiction="EE")
    e1 = await _make_employee(company_id, name="E1 Low Wage", base_rate=Decimal("500.00"))
    e2 = await _make_employee(
        company_id, name="E2 Pillar Elect",
        base_rate=Decimal("2000.00"), pillar_ii_rate_percent=Decimal("6.0"),
    )
    pay_run = await _make_pay_run(company_id)

    async with AsyncSessionLocal() as session:
        computed_e1 = await upsert_line(
            session,
            pay_run_id=pay_run.id,
            line_input=PayLineInput(
                employee_id=e1.id, ordinary_hours=Decimal("1"), overtime_hours=Decimal("0"),
            ),
            tenant_id=DEFAULT_TENANT_ID, actor="test",
        )
    async with AsyncSessionLocal() as session:
        computed_e2 = await upsert_line(
            session,
            pay_run_id=pay_run.id,
            line_input=PayLineInput(
                employee_id=e2.id, ordinary_hours=Decimal("1"), overtime_hours=Decimal("0"),
            ),
            tenant_id=DEFAULT_TENANT_ID, actor="test",
        )

    # Returned dataclass — the golden figures.
    assert computed_e1.gross == Decimal("500.00")
    assert computed_e1.payg == Decimal("0")          # AU column zeroed, not stale
    assert computed_e1.super_amount == Decimal("0")  # AU column zeroed, not stale
    assert computed_e1.ee_income_tax == Decimal("0.00")
    assert computed_e1.ee_unemployment_employee == Decimal("8.00")
    assert computed_e1.ee_unemployment_employer == Decimal("4.00")
    assert computed_e1.ee_social_tax == Decimal("292.38")  # 886 floor, not 165.00
    assert computed_e1.ee_pillar_ii == Decimal("10.00")    # 2% default

    assert computed_e2.gross == Decimal("2000.00")
    assert computed_e2.ee_pillar_ii == Decimal("120.00")   # 6% elected
    assert computed_e2.ee_unemployment_employee == Decimal("32.00")
    assert computed_e2.ee_unemployment_employer == Decimal("16.00")
    assert computed_e2.ee_social_tax == Decimal("660.00")
    assert computed_e2.ee_income_tax == Decimal("252.56")

    # Re-read from the DB — proves the migration's columns round-trip,
    # not just the in-memory dataclass.
    row_e1 = await _reload_line(pay_run.id, e1.id)
    assert row_e1.ee_social_tax == Decimal("292.38")
    assert row_e1.ee_pillar_ii == Decimal("10.00")
    assert row_e1.tax == Decimal("0.00")
    assert row_e1.super_amount == Decimal("0.00")

    row_e2 = await _reload_line(pay_run.id, e2.id)
    assert row_e2.ee_pillar_ii == Decimal("120.00")
    assert row_e2.ee_social_tax == Decimal("660.00")


async def test_ee_non_monthly_pay_frequency_refused() -> None:
    """Critic round 2 finding: ``_compute_ee`` never read
    ``employee.pay_frequency`` — the EUR 700/776 exemption and EUR 886
    social-tax floor are monthly figures, applied in full regardless of
    period length. Against the model's own WEEKLY default that silently
    overstated social tax ~4x. Now refused loudly instead of filed
    wrong."""
    from saebooks.services.pay_runs_v2 import PayRunV2Error

    company_id = await _make_ee_company(jurisdiction="EE")
    weekly_emp = await _make_employee(
        company_id, name="Weekly Paid", base_rate=Decimal("200.00"),
    )
    async with AsyncSessionLocal() as session:
        emp = await session.get(Employee, weekly_emp.id)
        emp.pay_frequency = PayFrequency.WEEKLY.value
        await session.commit()

    pay_run = await _make_pay_run(company_id)
    async with AsyncSessionLocal() as session:
        with pytest.raises(PayRunV2Error, match="MONTHLY"):
            await upsert_line(
                session,
                pay_run_id=pay_run.id,
                line_input=PayLineInput(
                    employee_id=weekly_emp.id, ordinary_hours=Decimal("1"),
                    overtime_hours=Decimal("0"),
                ),
                tenant_id=DEFAULT_TENANT_ID, actor="test",
            )


async def test_ee_mid_period_hire_refused() -> None:
    """Critic round 3 finding: ``_compute_ee`` read no employment-period
    input at all — the EUR 700/776 exemption and EUR 886 social-tax
    floor were applied in full even when ``Employee.start_date`` falls
    INSIDE the pay run's own period (a mid-month hire), overstating
    social tax on a partial-month gross exactly like the WEEKLY case
    above. Now refused loudly instead of filed wrong."""
    from saebooks.services.pay_runs_v2 import PayRunV2Error

    company_id = await _make_ee_company(jurisdiction="EE")
    mid_month_hire = await _make_employee(
        company_id, name="Mid Month Hire", base_rate=Decimal("20.00"),
    )
    async with AsyncSessionLocal() as session:
        emp = await session.get(Employee, mid_month_hire.id)
        emp.start_date = date(2026, 4, 20)  # inside the pay run's April period
        await session.commit()

    pay_run = await _make_pay_run(company_id)
    async with AsyncSessionLocal() as session:
        with pytest.raises(PayRunV2Error, match="partial-period"):
            await upsert_line(
                session,
                pay_run_id=pay_run.id,
                line_input=PayLineInput(
                    employee_id=mid_month_hire.id, ordinary_hours=Decimal("1"),
                    overtime_hours=Decimal("0"),
                ),
                tenant_id=DEFAULT_TENANT_ID, actor="test",
            )


async def test_ee_same_month_second_pay_run_refused() -> None:
    """Critic round 4 finding: the MONTHLY-only guard's own docstring
    flagged a sibling gap it left unfixed — two FINALIZED-or-DRAFT pay
    runs for one employee inside the same calendar month each get the
    EUR 886 social-tax floor applied independently (this compute is
    per-line, not per-employee-per-calendar-month), overstating the
    aggregate. Now the SECOND pay run's line is refused loudly instead
    of silently doubling the floor."""
    from saebooks.services.pay_runs_v2 import PayRunV2Error

    company_id = await _make_ee_company(jurisdiction="EE")
    emp = await _make_employee(company_id, name="Two Runs One Month", base_rate=Decimal("400.00"))

    pay_run_a = await _make_pay_run(company_id)
    async with AsyncSessionLocal() as session:
        await upsert_line(
            session,
            pay_run_id=pay_run_a.id,
            line_input=PayLineInput(
                employee_id=emp.id, ordinary_hours=Decimal("1"), overtime_hours=Decimal("0"),
            ),
            tenant_id=DEFAULT_TENANT_ID, actor="test",
        )

    # A second pay run, same company, same calendar month (both use the
    # module's fixed April 2026 period) — a correction/top-up run.
    pay_run_b = await _make_pay_run(company_id)
    async with AsyncSessionLocal() as session:
        with pytest.raises(PayRunV2Error, match="calendar month"):
            await upsert_line(
                session,
                pay_run_id=pay_run_b.id,
                line_input=PayLineInput(
                    employee_id=emp.id, ordinary_hours=Decimal("1"), overtime_hours=Decimal("0"),
                ),
                tenant_id=DEFAULT_TENANT_ID, actor="test",
            )

    # Re-running the SAME pay run for the SAME employee (idempotent
    # recalculate) must NOT trip the guard — only a DIFFERENT pay run
    # in the same month does.
    async with AsyncSessionLocal() as session:
        recomputed = await upsert_line(
            session,
            pay_run_id=pay_run_a.id,
            line_input=PayLineInput(
                employee_id=emp.id, ordinary_hours=Decimal("2"), overtime_hours=Decimal("0"),
            ),
            tenant_id=DEFAULT_TENANT_ID, actor="test",
        )
    assert recomputed.gross == Decimal("800.00")


async def test_au_pay_run_regression_untouched() -> None:
    """An AU-jurisdiction company still runs the pre-Packet-3 AU branch:
    real PAYG/super computed, EE columns left NULL."""
    company_id = await _make_ee_company(jurisdiction="AU")
    emp = await _make_employee(company_id, name="AU Employee", base_rate=Decimal("40.00"))
    pay_run = await _make_pay_run(company_id)

    async with AsyncSessionLocal() as session:
        computed = await upsert_line(
            session,
            pay_run_id=pay_run.id,
            line_input=PayLineInput(
                employee_id=emp.id, ordinary_hours=Decimal("152"), overtime_hours=Decimal("0"),
            ),
            tenant_id=DEFAULT_TENANT_ID, actor="test",
        )

    assert computed.gross == Decimal("6080.00")  # 152 * 40.00
    assert computed.ee_income_tax is None
    assert computed.ee_unemployment_employee is None
    assert computed.ee_social_tax is None
    assert computed.ee_pillar_ii is None
    # AU withholding still runs — some non-negative PAYG is computed
    # (exact figure is test_payg.py's concern, not this dispatch test's).
    assert computed.payg >= Decimal("0")

    row = await _reload_line(pay_run.id, emp.id)
    assert row.ee_income_tax is None
    assert row.ee_social_tax is None
    assert row.ee_pillar_ii is None
