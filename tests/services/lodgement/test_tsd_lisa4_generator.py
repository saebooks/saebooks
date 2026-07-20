"""TSD Lisa 4 (fringe benefits / erisoodustused) generator — Packet 2
tests (kmd-inf-tsd follow-up).

``generate_tsd_lisa4`` used to hard-refuse (``NotImplementedError`` —
"blocked on an EE erisoodustus event model"). This proves the real
generator against the SAME company-car golden
``tests/services/test_fringe_benefits_ee.py``'s
``test_company_car_standard_rate_hand_computed`` established (110 kW,
2 years old, standard rate): value 215.60 / income tax 60.81 / social
tax 91.22 — so this is a reconciliation to that packet's own asserted
numbers, not a re-derived parallel golden.

Uses ``finalize_ee_status_only`` (not the full GL-posting
``finalize_with_je``) — same convention ``test_tsd_generator.py``
already established for sourcing TSD from a FINALIZED EE pay run
without needing 13 settings-keyed GL accounts seeded; the GL-posting
path for fringe benefits has its own dedicated coverage in
``test_pay_runs_v2_ee_finalize.py``.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.services.lodgement.tsd.generator import generate_tsd_lisa4
from saebooks.services.pay_runs_v2 import (
    FringeBenefitLine,
    PayLineInput,
    finalize_ee_status_only,
    upsert_line,
)
from tests.services.test_pay_runs_v2_ee import (
    _PERIOD_END,
    _PERIOD_START,
    _make_employee,
    _make_pay_run,
)
from tests.services.test_tax_return_generator import _make_ee_company

pytestmark = pytest.mark.postgres_only


async def _post_line(
    pay_run_id: uuid.UUID, employee_id: uuid.UUID, *, fringe_benefits: tuple = (),
) -> None:
    async with AsyncSessionLocal() as session:
        await upsert_line(
            session,
            pay_run_id=pay_run_id,
            line_input=PayLineInput(
                employee_id=employee_id, ordinary_hours=Decimal("1"),
                overtime_hours=Decimal("0"), fringe_benefits=fringe_benefits,
            ),
            tenant_id=DEFAULT_TENANT_ID, actor="test",
        )


async def _finalize(pay_run_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        await finalize_ee_status_only(
            session, pay_run_id, tenant_id=DEFAULT_TENANT_ID, actor="test",
        )


async def test_lisa4_company_car_golden_row() -> None:
    """110 kW, 2-year-old car -> the standard-rate golden from
    ``test_fringe_benefits_ee.py``. transport_benefit / social_tax land
    on the confirmed fields; the income-tax total has no field
    populated (see generate_tsd_lisa4's own docstring flag)."""
    company_id = await _make_ee_company(jurisdiction="EE")
    emp = await _make_employee(company_id, name="Car Driver", base_rate=Decimal("500.00"))
    pay_run = await _make_pay_run(company_id)

    await _post_line(
        pay_run.id, emp.id,
        fringe_benefits=(
            FringeBenefitLine(
                benefit_category="motor_vehicle",
                engine_power_kw=Decimal("110"), car_age_years=2,
            ),
        ),
    )
    await _finalize(pay_run.id)

    async with AsyncSessionLocal() as session:
        header = await generate_tsd_lisa4(
            session, company_id=company_id,
            period_start=_PERIOD_START, period_end=_PERIOD_END,
        )

    assert header.transport_benefit == Decimal("215.60")
    assert header.total_expenses_incl_vat == Decimal("215.60")
    assert header.social_tax == Decimal("91.22")
    assert header.housing_benefit is None
    assert header.other_benefit is None
    # Income tax (60.81, computed — see PayRunLine.ee_fringe_benefit_
    # income_tax) is deliberately NOT slotted into any header field —
    # neither candidate (prior_period_income_tax / special_income_tax)
    # is confirmed to mean "this period's fringe-benefit income tax".
    assert header.prior_period_income_tax is None
    assert header.special_income_tax is None


async def test_lisa4_mixed_categories_bucket_correctly() -> None:
    """A car benefit (motor_vehicle -> c4040_Ts) and a generic cash
    benefit under an unrecognised category (-> c4050_Mv, the default
    bucket) on the SAME line sum into their respective fields, and
    total_expenses_incl_vat/social_tax are the grand totals across
    both."""
    company_id = await _make_ee_company(jurisdiction="EE")
    emp = await _make_employee(company_id, name="Mixed Benefits", base_rate=Decimal("500.00"))
    pay_run = await _make_pay_run(company_id)

    await _post_line(
        pay_run.id, emp.id,
        fringe_benefits=(
            FringeBenefitLine(
                benefit_category="motor_vehicle",
                engine_power_kw=Decimal("110"), car_age_years=2,
            ),
            FringeBenefitLine(
                benefit_category="entertainment", taxable_value=Decimal("100.00"),
            ),
        ),
    )
    await _finalize(pay_run.id)

    async with AsyncSessionLocal() as session:
        header = await generate_tsd_lisa4(
            session, company_id=company_id,
            period_start=_PERIOD_START, period_end=_PERIOD_END,
        )

    assert header.transport_benefit == Decimal("215.60")
    assert header.other_benefit == Decimal("100.00")
    assert header.total_expenses_incl_vat == Decimal("315.60")
    # 91.22 (car) + social tax on the 100.00 entertainment value:
    # 100 x 22/78 = 28.21 (income tax), (100 + 28.21) x 33% = 42.31.
    assert header.social_tax == Decimal("91.22") + Decimal("42.31")


async def test_lisa4_no_fringe_benefits_empty_header() -> None:
    """No fringe-benefit line anywhere in the period -> the untouched
    empty header (every field None), same as the pre-existing
    ``test_lisa4_empty_header_emits_empty_block`` serializer case."""
    company_id = await _make_ee_company(jurisdiction="EE")
    emp = await _make_employee(company_id, name="No Benefits", base_rate=Decimal("500.00"))
    pay_run = await _make_pay_run(company_id)

    await _post_line(pay_run.id, emp.id)
    await _finalize(pay_run.id)

    async with AsyncSessionLocal() as session:
        header = await generate_tsd_lisa4(
            session, company_id=company_id,
            period_start=_PERIOD_START, period_end=_PERIOD_END,
        )

    assert header.transport_benefit is None
    assert header.total_expenses_incl_vat is None
    assert header.social_tax is None
