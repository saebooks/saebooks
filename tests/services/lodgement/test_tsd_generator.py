"""TSD (income + social + withholding tax return) listing generator —
Packet 4 tests (``~/.claude/plans/kmd-inf-tsd-scope.md`` §6/§7).

Reuses the EXACT Packet-3 golden month
(``test_pay_runs_v2_ee.py``'s ``test_ee_golden_month_e1_and_e2``
scenario — same ``_make_employee``/``_make_pay_run`` helpers, same E1/
E2 figures) via the same cross-module import pattern
``test_kmd_inf_golden.py`` established, so this test proves the real
generator reconciles to Packet 3's own asserted numbers rather than
re-deriving a parallel golden by hand.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.services.lodgement.tsd import (
    PAYMENT_TYPE_WAGES,
    generate_tsd,
)
from saebooks.services.pay_runs_v2 import (
    PayLineInput,
    PayRunV2Error,
    finalize_ee_status_only,
    upsert_line,
)
from tests.services.test_pay_runs_v2_ee import (
    _PAYMENT_DATE,
    _PERIOD_END,
    _PERIOD_START,
    _make_employee,
    _make_pay_run,
)
from tests.services.test_tax_return_generator import _make_ee_company

pytestmark = pytest.mark.postgres_only

_E1_ISIKUKOOD = "38001010000"
_E2_ISIKUKOOD = "48505010001"


async def _post_line(pay_run_id: uuid.UUID, employee_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        await upsert_line(
            session,
            pay_run_id=pay_run_id,
            line_input=PayLineInput(
                employee_id=employee_id, ordinary_hours=Decimal("1"), overtime_hours=Decimal("0"),
            ),
            tenant_id=DEFAULT_TENANT_ID, actor="test",
        )


async def _finalize(pay_run_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        await finalize_ee_status_only(
            session, pay_run_id, tenant_id=DEFAULT_TENANT_ID, actor="test",
        )


async def test_tsd_golden_month_lisa1_and_main() -> None:
    """The scope §6 golden month: E1 low-wage/min-base-floor crosser,
    E2 pillar-II-6% elective. Asserts the full Lisa-1 row set (all 9
    fields per employee) + MAIN aggregates = Sigma Lisa 1."""
    company_id = await _make_ee_company(jurisdiction="EE")
    e1 = await _make_employee(
        company_id, name="E1 Low Wage", base_rate=Decimal("500.00"),
        isikukood=_E1_ISIKUKOOD,
    )
    e2 = await _make_employee(
        company_id, name="E2 Pillar Elect", base_rate=Decimal("2000.00"),
        pillar_ii_rate_percent=Decimal("6.0"), isikukood=_E2_ISIKUKOOD,
    )
    pay_run = await _make_pay_run(company_id)

    await _post_line(pay_run.id, e1.id)
    await _post_line(pay_run.id, e2.id)
    await _finalize(pay_run.id)

    async with AsyncSessionLocal() as session:
        listing = await generate_tsd(
            session, company_id=company_id, period_start=_PERIOD_START, period_end=_PERIOD_END,
        )

    assert not listing.errors
    assert len(listing.lisa1) == 2
    by_isikukood = {r.isikukood: r for r in listing.lisa1}

    r1 = by_isikukood[_E1_ISIKUKOOD]
    assert r1.employee_id == e1.id
    assert r1.payment_type_code == PAYMENT_TYPE_WAGES
    assert r1.gross == Decimal("500.00")
    assert r1.basic_exemption_applied == Decimal("700.00")
    assert r1.income_tax == Decimal("0.00")
    assert r1.unemployment_employee == Decimal("8.00")
    assert r1.unemployment_employer == Decimal("4.00")
    assert r1.social_tax == Decimal("292.38")  # 886 floor, not 165.00
    assert r1.pillar_ii == Decimal("10.00")    # 2% default
    assert r1.payment_date == _PAYMENT_DATE

    r2 = by_isikukood[_E2_ISIKUKOOD]
    assert r2.employee_id == e2.id
    assert r2.gross == Decimal("2000.00")
    assert r2.basic_exemption_applied == Decimal("700.00")
    assert r2.income_tax == Decimal("252.56")
    assert r2.unemployment_employee == Decimal("32.00")
    assert r2.unemployment_employer == Decimal("16.00")
    assert r2.social_tax == Decimal("660.00")
    assert r2.pillar_ii == Decimal("120.00")   # 6% elected

    main = listing.main
    assert main.employee_count == 2
    assert main.total_gross == Decimal("2500.00")
    assert main.total_income_tax == Decimal("252.56")
    assert main.total_unemployment_employee == Decimal("40.00")
    assert main.total_unemployment_employer == Decimal("20.00")
    assert main.total_social_tax == Decimal("952.38")
    assert main.total_pillar_ii == Decimal("130.00")

    # MAIN really is Sigma Lisa 1 (scope §2.2 "trivial roll-up") — not
    # independently computed.
    assert main.total_gross == sum((r.gross for r in listing.lisa1), Decimal("0"))
    assert main.total_income_tax == sum((r.income_tax for r in listing.lisa1), Decimal("0"))
    assert main.total_social_tax == sum((r.social_tax for r in listing.lisa1), Decimal("0"))

    # Critic round 2 finding: finalize_ee_status_only never posts a
    # journal entry, so this pay run (the sole source above) has no GL
    # backing — surfaced, not silently omitted.
    assert listing.gl_not_posted_pay_run_ids == [pay_run.id]


async def test_tsd_missing_isikukood_is_data_quality_error_not_silent_drop() -> None:
    """Mirrors KMD-INF's no-registration-number case (scope §2.1) —
    generator.py module docstring point 4."""
    company_id = await _make_ee_company(jurisdiction="EE")
    no_id = await _make_employee(
        company_id, name="No Isikukood", base_rate=Decimal("500.00"),
    )  # isikukood omitted
    pay_run = await _make_pay_run(company_id)
    await _post_line(pay_run.id, no_id.id)
    await _finalize(pay_run.id)

    async with AsyncSessionLocal() as session:
        listing = await generate_tsd(
            session, company_id=company_id, period_start=_PERIOD_START, period_end=_PERIOD_END,
        )

    assert listing.lisa1 == []
    assert len(listing.errors) == 1
    assert listing.errors[0].employee_id == no_id.id
    assert "isikukood" in listing.errors[0].message


async def test_tsd_corrupt_isikukood_is_data_quality_error_not_raise() -> None:
    """Critic round 3 finding: ``decrypt_isikukood`` ->
    ``services.crypto.decrypt_field`` raises ``FieldDecryptionError`` on
    a corrupt/wrong-key ciphertext — was unguarded, so one bad employee
    row aborted the WHOLE company's TSD generation. Every other bad-line
    case in this module degrades to a ``TsdDataQualityError`` (module
    docstring point 4); this must too."""
    from sqlalchemy import update

    from saebooks.models.employee import Employee

    company_id = await _make_ee_company(jurisdiction="EE")
    emp = await _make_employee(
        company_id, name="Corrupt Isikukood", base_rate=Decimal("500.00"),
        isikukood=_E1_ISIKUKOOD,
    )
    pay_run = await _make_pay_run(company_id)
    await _post_line(pay_run.id, emp.id)
    await _finalize(pay_run.id)

    async with AsyncSessionLocal() as session:
        await session.execute(
            update(Employee)
            .where(Employee.id == emp.id)
            .values(isikukood_encrypted="not-a-valid-fernet-token")
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        listing = await generate_tsd(
            session, company_id=company_id, period_start=_PERIOD_START, period_end=_PERIOD_END,
        )

    assert listing.lisa1 == []
    assert len(listing.errors) == 1
    assert listing.errors[0].employee_id == emp.id
    assert "decrypt" in listing.errors[0].message.lower()


async def test_tsd_excludes_draft_pay_run_lines() -> None:
    """"Posted" only — a DRAFT (never finalized) EE pay run's lines
    must not appear, mirroring KMD-INF's POSTED-only document filter."""
    company_id = await _make_ee_company(jurisdiction="EE")
    emp = await _make_employee(
        company_id, name="Never Finalized", base_rate=Decimal("500.00"),
        isikukood=_E1_ISIKUKOOD,
    )
    pay_run = await _make_pay_run(company_id)
    await _post_line(pay_run.id, emp.id)
    # deliberately NOT finalized

    async with AsyncSessionLocal() as session:
        listing = await generate_tsd(
            session, company_id=company_id, period_start=_PERIOD_START, period_end=_PERIOD_END,
        )

    assert listing.lisa1 == []
    assert listing.errors == []
    assert listing.main.employee_count == 0


async def test_tsd_excludes_au_lines_even_if_finalized() -> None:
    """An AU pay run's lines have ``ee_income_tax IS NULL`` (Packet 3's
    own AU-regression assertion, ``test_au_pay_run_regression_untouched``)
    and must never surface as TSD rows even if the run reaches
    FINALIZED (status set directly here — this test's concern is the
    generator's ``ee_income_tax IS NOT NULL`` filter, not
    ``finalize_with_je``'s AU-CoA-dependent JE-building, which
    ``test_pay_runs_v2_ee.py`` already covers)."""
    from sqlalchemy import update

    from saebooks.models.pay_run import PayRun

    company_id = await _make_ee_company(jurisdiction="AU")
    emp = await _make_employee(
        company_id, name="AU Employee", base_rate=Decimal("40.00"),
    )
    pay_run = await _make_pay_run(company_id)
    async with AsyncSessionLocal() as session:
        await upsert_line(
            session, pay_run_id=pay_run.id,
            line_input=PayLineInput(
                employee_id=emp.id, ordinary_hours=Decimal("152"), overtime_hours=Decimal("0"),
            ),
            tenant_id=DEFAULT_TENANT_ID, actor="test",
        )

    async with AsyncSessionLocal() as session:
        await session.execute(
            update(PayRun).where(PayRun.id == pay_run.id).values(status="finalized")
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        listing = await generate_tsd(
            session, company_id=company_id, period_start=_PERIOD_START, period_end=_PERIOD_END,
        )

    assert listing.lisa1 == []
    assert listing.main.employee_count == 0


async def test_finalize_ee_status_only_locks_lines_no_je() -> None:
    """finalize_ee_status_only flips status without a journal_id, and
    upsert_line then refuses further writes (the same DRAFT-only lock
    ``finalize_with_je`` relies on for AU)."""
    company_id = await _make_ee_company(jurisdiction="EE")
    emp = await _make_employee(
        company_id, name="Lock Test", base_rate=Decimal("500.00"), isikukood=_E1_ISIKUKOOD,
    )
    pay_run = await _make_pay_run(company_id)
    await _post_line(pay_run.id, emp.id)

    async with AsyncSessionLocal() as session:
        finalized = await finalize_ee_status_only(
            session, pay_run.id, tenant_id=DEFAULT_TENANT_ID, actor="test",
        )
    assert finalized.journal_id is None
    assert finalized.status == "finalized"

    with pytest.raises(PayRunV2Error, match="only DRAFT accepts"):
        await _post_line(pay_run.id, emp.id)


async def test_finalize_ee_status_only_rejects_au_company() -> None:
    company_id = await _make_ee_company(jurisdiction="AU")
    emp = await _make_employee(company_id, name="AU Employee", base_rate=Decimal("40.00"))
    pay_run = await _make_pay_run(company_id)
    async with AsyncSessionLocal() as session:
        await upsert_line(
            session, pay_run_id=pay_run.id,
            line_input=PayLineInput(
                employee_id=emp.id, ordinary_hours=Decimal("1"), overtime_hours=Decimal("0"),
            ),
            tenant_id=DEFAULT_TENANT_ID, actor="test",
        )

    async with AsyncSessionLocal() as session:
        with pytest.raises(PayRunV2Error, match="EE-jurisdiction companies only"):
            await finalize_ee_status_only(
                session, pay_run.id, tenant_id=DEFAULT_TENANT_ID, actor="test",
            )
