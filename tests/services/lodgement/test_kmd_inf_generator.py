"""KMD-INF listing generator — Packet 1 tests
(``~/.claude/plans/kmd-inf-tsd-scope.md`` §6/§7).

Two groups:

* Pure-unit, no DB — the small pure functions (erisuse-kood derivation,
  the two swappable crossing-sum strategies, the reporting-type -> KMD
  box map).
* ``postgres_only`` golden period — reuses ``_make_ee_company`` from
  ``tests/services/test_tax_return_generator.py`` (same cross-module
  import pattern ``test_kmd_golden.py`` already established) so the
  company's tax codes match the KMD box engine's own EE seed
  conventions, posts REAL invoices/credit-notes/bills through the
  record-type services (not raw journal entries — KMD-INF reads
  Invoice/CreditNote/Bill tables directly), and reconciles the listed
  rows against ``_kmd_box_vector`` (the same box aggregator
  ``test_kmd_golden.py`` uses) to prove INF is a *subset* view of KMD,
  not an independent total.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.contact import Contact, ContactType
from saebooks.models.tax_code import TaxCode
from saebooks.services import bills as bills_svc
from saebooks.services import credit_notes as credit_notes_svc
from saebooks.services import invoices as invoices_svc
from saebooks.services.lodgement.kmd_inf.generator import (
    REPORTING_TYPE_TO_KMD_BOX,
    KmdInfCompanyConfigError,
    _crossing_sum_net,
    _crossing_sum_separate,
    _erisuse_a,
    _erisuse_b,
    generate_kmd_inf,
)
from tests.services.test_tax_return_generator import _kmd_box_vector, _make_ee_company

# asyncio_mode = "auto" (pyproject.toml) — no module-level asyncio marker
# needed. Only the DB-backed golden test is postgres_only; the pure-unit
# tests above run without a database.

_D = Decimal


# ---------------------------------------------------------------------------
# Pure-unit — no DB.
# ---------------------------------------------------------------------------


def test_reporting_type_to_box_matches_seed_disposition() -> None:
    """scope §2.1: standard->box1, reduced_9->box2, reduced_13->box2-2."""
    assert REPORTING_TYPE_TO_KMD_BOX["standard"] == "1"
    assert REPORTING_TYPE_TO_KMD_BOX["reduced_9"] == "2"
    assert REPORTING_TYPE_TO_KMD_BOX["reduced_13"] == "2-2"
    # zero/exempt/RC types deliberately have no KMD-INF Part A box —
    # they don't feed boxes 1/2/2-2.
    assert "exempt" not in REPORTING_TYPE_TO_KMD_BOX
    assert "zero_export" not in REPORTING_TYPE_TO_KMD_BOX


def test_erisuse_a_mixed_rate_wins_over_rc_domestic_supply() -> None:
    assert _erisuse_a({"standard"}) is None
    assert _erisuse_a({"rc_domestic_supply"}) == "02"
    assert _erisuse_a({"standard", "reduced_9"}) == "03"
    # mixed-rate check takes priority even if rc_domestic_supply is one
    # of the mixed types.
    assert _erisuse_a({"standard", "rc_domestic_supply"}) == "03"


def test_erisuse_b_reverse_charge_types() -> None:
    assert _erisuse_b("rc_eu_acq_goods") == "12"
    assert _erisuse_b("rc_eu_acq_services") == "12"
    assert _erisuse_b("rc_domestic_acq") == "12"
    assert _erisuse_b("standard") is None
    # §30 partial deduction (11) and §41/§42 special scheme (01) are
    # never derived — no reporting_type tag exists for either
    # (scope's own flag; see generator.py module docstring point 4).


def test_crossing_sum_separate_excludes_credit_notes() -> None:
    """Default strategy — P1's golden scenario: 700+500 invoices,
    -300 credit note. 'separate' crosses on invoices alone (1200)."""
    assert _crossing_sum_separate([_D("700"), _D("500")], [_D("300")]) == _D("1200")


def test_crossing_sum_net_nets_credit_notes() -> None:
    """Alternate strategy (NOT default) — same P1 scenario nets to 900,
    i.e. would NOT cross €1,000 under 'net'. This is exactly why
    generator.py's default is 'separate', not 'net' — see its module
    docstring point 1."""
    assert _crossing_sum_net([_D("700"), _D("500")], [_D("300")]) == _D("900")


# ---------------------------------------------------------------------------
# Golden period — postgres_only.
# ---------------------------------------------------------------------------

_PERIOD_START = date(2026, 2, 1)
_PERIOD_END = date(2026, 2, 28)


async def _contact(company_id: uuid.UUID, name: str, contact_type: ContactType, reg_no: str | None) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        c = Contact(
            company_id=company_id, name=name, contact_type=contact_type,
            registration_number=reg_no,
        )
        session.add(c)
        await session.commit()
        return c.id


async def _post_invoice(
    company_id, contact_id, income_account_id, tax_code_id, *,
    net: Decimal, issue_date: date, settlement_date: date | None = None,
    currency: str = "EUR", fx_rate: Decimal | None = None,
) -> None:
    async with AsyncSessionLocal() as session:
        inv = await invoices_svc.create_draft(
            session, company_id=company_id, contact_id=contact_id,
            issue_date=issue_date, due_date=issue_date, settlement_date=settlement_date,
            currency=currency, fx_rate=fx_rate,
            lines=[{
                "description": "KMD-INF golden sale", "account_id": income_account_id,
                "tax_code_id": tax_code_id, "quantity": Decimal("1"), "unit_price": net,
            }],
        )
        await invoices_svc.post_invoice(session, inv.id, posted_by="pytest-kmd-inf")


async def _post_credit_note(company_id, contact_id, income_account_id, tax_code_id, *, net: Decimal, issue_date: date) -> None:
    async with AsyncSessionLocal() as session:
        cn = await credit_notes_svc.create_draft(
            session, company_id=company_id, contact_id=contact_id, issue_date=issue_date,
            lines=[{
                "description": "KMD-INF golden credit note", "account_id": income_account_id,
                "tax_code_id": tax_code_id, "quantity": Decimal("1"), "unit_price": net,
            }],
        )
        await credit_notes_svc.post_credit_note(session, cn.id, posted_by="pytest-kmd-inf")


async def _post_bill(
    company_id, contact_id, expense_account_id, tax_code_id, *,
    net: Decimal, issue_date: date, currency: str = "EUR", fx_rate: Decimal | None = None,
) -> None:
    async with AsyncSessionLocal() as session:
        bill = await bills_svc.create_draft(
            session, company_id=company_id, contact_id=contact_id,
            issue_date=issue_date, due_date=issue_date,
            currency=currency, fx_rate=fx_rate,
            lines=[{
                "description": "KMD-INF golden purchase", "account_id": expense_account_id,
                "tax_code_id": tax_code_id, "quantity": Decimal("1"), "unit_price": net,
            }],
        )
        await bills_svc.post_bill(session, bill.id, posted_by="pytest-kmd-inf")


@pytest.mark.postgres_only
async def test_kmd_inf_refuses_non_eur_base_currency() -> None:
    """Critic round 3 finding: ``Company.base_currency`` is a free-form
    column fully independent of ``Company.jurisdiction`` — nothing
    previously stopped an EE-jurisdiction company provisioned with a
    non-EUR base_currency from having its raw (non-EUR) ledger figures
    silently emitted against the EUR-denominated €1,000 threshold and
    every taxable-value/input-VAT column. Now refused loudly."""
    from sqlalchemy import update

    from saebooks.models.company import Company

    company_id = await _make_ee_company(jurisdiction="EE")
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(Company).where(Company.id == company_id).values(base_currency="AUD")
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        with pytest.raises(KmdInfCompanyConfigError, match="EUR"):
            await generate_kmd_inf(
                session, company_id=company_id,
                period_start=date(2026, 4, 1), period_end=date(2026, 4, 30),
            )


@pytest.mark.postgres_only
async def test_kmd_inf_golden_period() -> None:
    company_id = await _make_ee_company(jurisdiction="EE")

    async with AsyncSessionLocal() as session:
        by_code = {
            code: aid for code, aid in (
                await session.execute(select(Account.code, Account.id).where(Account.company_id == company_id))
            ).all()
        }
        tax_by_type = {
            rt: tid for rt, tid in (
                await session.execute(select(TaxCode.reporting_type, TaxCode.id).where(TaxCode.company_id == company_id))
            ).all()
        }
        # A non-deductible tax code (0% exempt, input_credit_recoverable
        # False) — _make_ee_company's own "exempt" code defaults
        # input_credit_recoverable=True (the model default), so this
        # test needs its own explicitly-non-deductible code to exercise
        # the Part B "excluded even though partner crosses" scenario.
        nd_tc = TaxCode(
            company_id=company_id,
            code="EE-EXEMPT-ND", name="EE exempt (non-deductible)", rate=Decimal("0.000"),
            tax_system="VAT", jurisdiction="EE", reporting_type="exempt",
            input_credit_recoverable=False,
        )
        session.add(nd_tc)
        # AR/AP control accounts — _make_ee_company posts via raw
        # journal entries and never needed these; invoices_svc/bills_svc
        # (real record-type posting, required so KMD-INF can read real
        # Invoice/Bill rows) both hard-require them.
        session.add(Account(company_id=company_id, code="1200", name="Trade Debtors", account_type=AccountType.ASSET))
        session.add(Account(company_id=company_id, code="2100", name="Trade Creditors", account_type=AccountType.LIABILITY))
        await session.commit()
        nd_tax_code_id = nd_tc.id

    income_acct = by_code["4-1000"]
    expense_acct = by_code["5-1000"]
    standard_tc = tax_by_type["standard"]

    p1 = await _contact(company_id, "P1 Straddles Both Ways", ContactType.CUSTOMER, "10111111")
    p2 = await _contact(company_id, "P2 Stays Under", ContactType.CUSTOMER, "10222222")
    p3 = await _contact(company_id, "P3 Boundary", ContactType.CUSTOMER, "10333333")
    no_code = await _contact(company_id, "No-Code Customer", ContactType.CUSTOMER, None)
    s1 = await _contact(company_id, "S1 Deductible Supplier", ContactType.SUPPLIER, "10555555")
    s2 = await _contact(company_id, "S2 Stays Under", ContactType.SUPPLIER, "10666666")

    # --- Part A data -----------------------------------------------------
    await _post_invoice(company_id, p1, income_acct, standard_tc, net=Decimal("700.00"), issue_date=date(2026, 2, 5))
    await _post_invoice(company_id, p1, income_acct, standard_tc, net=Decimal("500.00"), issue_date=date(2026, 2, 10))
    await _post_credit_note(company_id, p1, income_acct, standard_tc, net=Decimal("300.00"), issue_date=date(2026, 2, 15))
    await _post_invoice(company_id, p2, income_acct, standard_tc, net=Decimal("800.00"), issue_date=date(2026, 2, 6))
    await _post_invoice(company_id, p3, income_acct, standard_tc, net=Decimal("1000.00"), issue_date=date(2026, 2, 7))
    await _post_invoice(company_id, no_code, income_acct, standard_tc, net=Decimal("1300.00"), issue_date=date(2026, 2, 8))

    # --- Part B data -------------------------------------------------------
    await _post_bill(company_id, s1, expense_acct, standard_tc, net=Decimal("1100.00"), issue_date=date(2026, 2, 9))
    await _post_bill(company_id, s1, expense_acct, nd_tax_code_id, net=Decimal("400.00"), issue_date=date(2026, 2, 11))
    await _post_bill(company_id, s2, expense_acct, standard_tc, net=Decimal("900.00"), issue_date=date(2026, 2, 12))

    async with AsyncSessionLocal() as session:
        listing = await generate_kmd_inf(
            session, company_id=company_id, period_start=_PERIOD_START, period_end=_PERIOD_END,
        )

    # --- Part A assertions ---------------------------------------------
    by_partner_a: dict[str, list] = {}
    for row in listing.part_a:
        by_partner_a.setdefault(row.partner_registration_number, []).append(row)

    assert "10111111" in by_partner_a, "P1 must cross via invoices alone (1200 >= 1000)"
    p1_rows = by_partner_a["10111111"]
    assert len(p1_rows) == 3  # 2 invoices + 1 credit note (all single-rate -> 1 row each)
    cn_rows = [r for r in p1_rows if r.is_credit_note]
    assert len(cn_rows) == 1
    assert cn_rows[0].taxable_value == Decimal("-300.00"), "credit-note row must be SIGNED negative"
    assert all(r.kmd_box_code == "1" for r in p1_rows)
    assert all(r.erisuse_kood is None for r in p1_rows), "single-rate documents carry no erisuse marker"

    assert "10222222" not in by_partner_a, "P2 (800 ex-VAT) stays under threshold — not listed"

    assert "10333333" in by_partner_a, "P3 (exactly 1000.00) is the inclusive >= boundary"
    assert len(by_partner_a["10333333"]) == 1
    assert by_partner_a["10333333"][0].taxable_value == Decimal("1000.00")

    # No-code customer: crosses (1300) but has no registration_number ->
    # data-quality error, NOT a silent drop, and NOT listed as rows.
    assert not any(r.partner_name == "No-Code Customer" for r in listing.part_a)
    dq_errors = [e for e in listing.errors if e.part == "A"]
    assert len(dq_errors) == 1
    assert dq_errors[0].partner_name == "No-Code Customer"
    assert dq_errors[0].period_total_ex_vat == Decimal("1300.00")

    # --- Part B assertions -----------------------------------------------
    by_partner_b: dict[str, list] = {}
    for row in listing.part_b:
        by_partner_b.setdefault(row.partner_registration_number, []).append(row)

    assert "10555555" in by_partner_b, "S1 crosses via its deductible bill alone (1100 >= 1000)"
    s1_rows = by_partner_b["10555555"]
    assert len(s1_rows) == 1, "the non-deductible bill must be EXCLUDED even though S1 is listed"
    assert s1_rows[0].input_vat == Decimal("264.00")  # 1100 * 24%
    assert s1_rows[0].document_total_incl_vat == Decimal("1364.00")

    assert "10666666" not in by_partner_b, "S2 (900 ex-VAT deductible) stays under threshold"
    assert not any(e.part == "B" for e in listing.errors)

    # --- Reconciliation: INF is a SUBSET of the same period's KMD boxes --
    box_amounts = await _kmd_box_vector(company_id, from_date=_PERIOD_START, to_date=_PERIOD_END)
    box1_total = box_amounts["1"]
    box5_total = box_amounts["5"]

    listed_taxable_sum = sum((r.taxable_value for r in listing.part_a if r.kmd_box_code == "1"), Decimal("0"))
    listed_input_vat_sum = sum((r.input_vat for r in listing.part_b), Decimal("0"))

    # Box 1 includes P2 (800) and No-Code (1300) too — neither of which
    # is in the listed subset — so the box total must exceed the listed
    # sum, proving INF is a filtered VIEW, not the KMD total itself
    # (scope §6: "proves INF is a *subset* view, not the KMD total").
    assert box1_total == Decimal("4000.00")  # 1200 - 300 + 800 + 1000 + 1300
    assert listed_taxable_sum == Decimal("1900.00")  # (700+500-300) + 1000
    assert listed_taxable_sum < box1_total

    # Box 5 includes S2's 216.00 too (not listed) -> same subset property.
    assert box5_total == Decimal("480.00")  # 264 (S1) + 216 (S2)
    assert listed_input_vat_sum == Decimal("264.00")
    assert listed_input_vat_sum < box5_total


@pytest.mark.postgres_only
async def test_kmd_inf_period_basis_uses_settlement_date_override() -> None:
    """scope §2.1: period basis is settlement_date when set, else
    issue_date — mirrors services/invoices.py's own gl_entry_date rule
    so INF and KMD never drift on which period a document lands in.
    Exercises BOTH directions of the override (pulled in, pushed out) —
    the main golden test above never sets settlement_date at all."""
    company_id = await _make_ee_company(jurisdiction="EE")
    async with AsyncSessionLocal() as session:
        session.add(Account(company_id=company_id, code="1200", name="Trade Debtors", account_type=AccountType.ASSET))
        await session.commit()
        by_code = {
            code: aid for code, aid in (
                await session.execute(select(Account.code, Account.id).where(Account.company_id == company_id))
            ).all()
        }
        tax_by_type = {
            rt: tid for rt, tid in (
                await session.execute(select(TaxCode.reporting_type, TaxCode.id).where(TaxCode.company_id == company_id))
            ).all()
        }
    income_acct = by_code["4-1000"]
    standard_tc = tax_by_type["standard"]

    pulled_in = await _contact(company_id, "Pulled-In by settlement_date", ContactType.CUSTOMER, "10777777")
    pushed_out = await _contact(company_id, "Pushed-Out by settlement_date", ContactType.CUSTOMER, "10888888")

    # issue_date is OUTSIDE the period; settlement_date pulls it IN.
    await _post_invoice(
        company_id, pulled_in, income_acct, standard_tc, net=Decimal("1500.00"),
        issue_date=date(2026, 1, 20), settlement_date=date(2026, 2, 10),
    )
    # issue_date is INSIDE the period; settlement_date pushes it OUT.
    await _post_invoice(
        company_id, pushed_out, income_acct, standard_tc, net=Decimal("1500.00"),
        issue_date=date(2026, 2, 10), settlement_date=date(2026, 3, 5),
    )

    async with AsyncSessionLocal() as session:
        listing = await generate_kmd_inf(
            session, company_id=company_id, period_start=_PERIOD_START, period_end=_PERIOD_END,
        )

    reg_nos = {row.partner_registration_number for row in listing.part_a}
    assert "10777777" in reg_nos, "settlement_date must PULL a document into the period"
    assert "10888888" not in reg_nos, "settlement_date must PUSH a document out of the period"


@pytest.mark.postgres_only
async def test_kmd_inf_untagged_lines_surface_data_quality_error_not_silent_drop() -> None:
    """Critic round 1 finding: an invoice whose every line has
    ``tax_code_id=None`` still counts its full value toward the €1,000
    crossing test (``threshold_amount`` = the whole-document
    ``base_subtotal``), but a line with no resolvable tax code produces
    no ``_LineGroup`` and therefore no row. Before the fix, the partner
    crossed, passed the reg_no check, and then emitted ZERO Part A rows
    with no error recorded anywhere. Now it must surface a
    ``KmdInfDataQualityError`` instead."""
    company_id = await _make_ee_company(jurisdiction="EE")
    async with AsyncSessionLocal() as session:
        session.add(Account(company_id=company_id, code="1200", name="Trade Debtors", account_type=AccountType.ASSET))
        await session.commit()
        by_code = {
            code: aid for code, aid in (
                await session.execute(select(Account.code, Account.id).where(Account.company_id == company_id))
            ).all()
        }
    income_acct = by_code["4-1000"]

    untagged = await _contact(company_id, "Untagged-Line Customer", ContactType.CUSTOMER, "10999999")
    await _post_invoice(company_id, untagged, income_acct, None, net=Decimal("1500.00"), issue_date=date(2026, 2, 5))

    async with AsyncSessionLocal() as session:
        listing = await generate_kmd_inf(
            session, company_id=company_id, period_start=_PERIOD_START, period_end=_PERIOD_END,
        )

    assert not any(r.partner_registration_number == "10999999" for r in listing.part_a), (
        "an untagged line resolves to zero rows — no _LineGroup can be produced"
    )
    dq_errors = [e for e in listing.errors if e.part == "A" and e.partner_name == "Untagged-Line Customer"]
    assert len(dq_errors) == 1, "the crossed-but-zero-rows partner must be surfaced, not silently dropped"
    assert dq_errors[0].period_total_ex_vat == Decimal("1500.00")


@pytest.mark.postgres_only
async def test_kmd_inf_credit_note_only_partner_surfaces_data_quality_error() -> None:
    """Critic round 1 finding: a partner whose ONLY period activity is a
    standalone credit note (no invoice) can never cross the €1,000
    threshold under the default 'separate' strategy — ``invoice_totals``
    is empty so ``crossing`` is always 0. Before the fix this partner
    vanished from Part A with no trace; now it must surface a
    ``KmdInfDataQualityError``."""
    company_id = await _make_ee_company(jurisdiction="EE")
    async with AsyncSessionLocal() as session:
        session.add(Account(company_id=company_id, code="1200", name="Trade Debtors", account_type=AccountType.ASSET))
        await session.commit()
        by_code = {
            code: aid for code, aid in (
                await session.execute(select(Account.code, Account.id).where(Account.company_id == company_id))
            ).all()
        }
        tax_by_type = {
            rt: tid for rt, tid in (
                await session.execute(select(TaxCode.reporting_type, TaxCode.id).where(TaxCode.company_id == company_id))
            ).all()
        }
    income_acct = by_code["4-1000"]
    standard_tc = tax_by_type["standard"]

    cn_only = await _contact(company_id, "Credit-Note-Only Partner", ContactType.CUSTOMER, "10123123")
    await _post_credit_note(company_id, cn_only, income_acct, standard_tc, net=Decimal("5000.00"), issue_date=date(2026, 2, 15))

    async with AsyncSessionLocal() as session:
        listing = await generate_kmd_inf(
            session, company_id=company_id, period_start=_PERIOD_START, period_end=_PERIOD_END,
        )

    assert not any(r.partner_registration_number == "10123123" for r in listing.part_a), (
        "a credit-note-only partner never crosses via the default 'separate' strategy"
    )
    dq_errors = [e for e in listing.errors if e.part == "A" and e.partner_name == "Credit-Note-Only Partner"]
    assert len(dq_errors) == 1, "must be surfaced, not silently invisible"
    assert dq_errors[0].period_total_ex_vat == Decimal("5000.00")


@pytest.mark.postgres_only
async def test_kmd_inf_converts_foreign_currency_lines_to_base_currency() -> None:
    """Critic round 4 finding: ``InvoiceLine``/``BillLine.line_subtotal``/
    ``line_tax`` are DOCUMENT-currency amounts (no per-line base
    column exists) — before the fix these were summed directly into
    ``taxable_value``/``input_vat``/the €1,000 crossing test with no
    fx-rate conversion, so a non-EUR-denominated document silently
    mislabelled its raw foreign-currency figures as EUR.

    Part A: a USD invoice, fx_rate=0.90 (net 1200.00 USD = EUR 1080.00)
    must cross the EUR 1,000 threshold on its BASE-currency total and
    the row's ``taxable_value``/``document_total_ex_vat`` must both
    read EUR 1080.00 — not the raw 1200.00 USD figure (which would
    itself mismatch ``document_total_ex_vat``, already base-converted
    via ``base_subtotal``, breaking the single-row-single-currency
    invariant even before the threshold question).

    Part B: a USD bill, fx_rate=0.60, net 1050.00 USD = EUR 630.00 —
    BELOW the threshold on the converted figure though ABOVE it on the
    raw 1050.00 — must NOT be listed. Proves the fix isn't merely
    "convert the display columns" but actually gates the crossing
    test on the converted sum."""
    company_id = await _make_ee_company(jurisdiction="EE")
    async with AsyncSessionLocal() as session:
        session.add(Account(company_id=company_id, code="1200", name="Trade Debtors", account_type=AccountType.ASSET))
        session.add(Account(company_id=company_id, code="2100", name="Trade Creditors", account_type=AccountType.LIABILITY))
        await session.commit()
        by_code = {
            code: aid for code, aid in (
                await session.execute(select(Account.code, Account.id).where(Account.company_id == company_id))
            ).all()
        }
        tax_by_type = {
            rt: tid for rt, tid in (
                await session.execute(select(TaxCode.reporting_type, TaxCode.id).where(TaxCode.company_id == company_id))
            ).all()
        }
    income_acct = by_code["4-1000"]
    expense_acct = by_code["5-1000"]
    standard_tc = tax_by_type["standard"]

    usd_customer = await _contact(company_id, "USD Customer", ContactType.CUSTOMER, "10444444")
    usd_supplier = await _contact(company_id, "USD Supplier Below Threshold", ContactType.SUPPLIER, "10777777")

    await _post_invoice(
        company_id, usd_customer, income_acct, standard_tc,
        net=Decimal("1200.00"), issue_date=date(2026, 2, 5),
        currency="USD", fx_rate=Decimal("0.90"),
    )
    await _post_bill(
        company_id, usd_supplier, expense_acct, standard_tc,
        net=Decimal("1050.00"), issue_date=date(2026, 2, 6),
        currency="USD", fx_rate=Decimal("0.60"),
    )

    async with AsyncSessionLocal() as session:
        listing = await generate_kmd_inf(
            session, company_id=company_id, period_start=_PERIOD_START, period_end=_PERIOD_END,
        )

    a_rows = [r for r in listing.part_a if r.partner_registration_number == "10444444"]
    assert len(a_rows) == 1, "USD invoice must cross on its EUR-converted total (1080 >= 1000)"
    assert a_rows[0].taxable_value == Decimal("1080.00"), "taxable_value must be base-currency EUR, not raw USD 1200.00"
    assert a_rows[0].document_total_ex_vat == Decimal("1080.00")

    assert not any(r.partner_registration_number == "10777777" for r in listing.part_b), (
        "USD bill converts to EUR 630.00 (below threshold) even though the raw "
        "USD figure (1050.00) would incorrectly cross"
    )
