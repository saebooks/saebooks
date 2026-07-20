"""2027 data-based KMD generator — DB-backed (postgres_only).

Reuses the KMD-INF golden-period posting helpers (same cross-module import
pattern as ``test_kmd_inf_golden.py``) to prove the transaction-listing
generator over a REAL posted ledger: threshold OFF (small partners still
listed), a KMDTYYP code per row, credit notes signed, ordinary input → O_101,
and end-to-end koondvaade reconciliation against the box engine.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.contact import ContactType
from saebooks.models.tax_code import TaxCode
from saebooks.services.lodgement.kmd_2027.generator import generate_kmd_2027
from saebooks.services.lodgement.kmd_2027.reconcile import reconcile, reconcile_period
from saebooks.services.reference.loader import load_seeds
from tests.services.lodgement.test_kmd_inf_generator import (
    _PERIOD_END,
    _PERIOD_START,
    _contact,
    _post_bill,
    _post_credit_note,
    _post_invoice,
)
from tests.services.test_tax_return_generator import (
    _kmd_box_vector,
    _make_ee_company,
    _reference_db_configured,
)

pytestmark = pytest.mark.postgres_only

_D = Decimal


async def _accounts_and_codes(company_id):
    async with AsyncSessionLocal() as session:
        from saebooks.models.account import Account
        by_code = {
            code: aid for code, aid in (
                await session.execute(
                    select(Account.code, Account.id).where(Account.company_id == company_id)
                )
            ).all()
        }
        tax_by_type = {
            rt: tid for rt, tid in (
                await session.execute(
                    select(TaxCode.reporting_type, TaxCode.id).where(TaxCode.company_id == company_id)
                )
            ).all()
        }
    return by_code, tax_by_type


async def _ee_company_with_controls():
    """``_make_ee_company`` + the AR/AP control accounts that real record-type
    posting (invoices_svc / bills_svc) hard-requires. ``_make_ee_company`` posts
    via raw journal entries and never seeds these, so — exactly as
    ``test_kmd_inf_generator`` does inline — we add them before posting any
    Invoice/Bill the generator must read back."""
    cid = await _make_ee_company(jurisdiction="EE")
    async with AsyncSessionLocal() as session:
        session.add(Account(company_id=cid, code="1200", name="Trade Debtors", account_type=AccountType.ASSET))
        session.add(Account(company_id=cid, code="2100", name="Trade Creditors", account_type=AccountType.LIABILITY))
        await session.commit()
    return cid


async def test_threshold_off_lists_every_transaction_with_a_kmdtyyp_code() -> None:
    """A small (<€1,000) standard sale — which KMD-INF would DROP — is still
    exported, coded M_101, with the taxable value and rate."""
    company_id = await _ee_company_with_controls()
    by_code, tax_by_type = await _accounts_and_codes(company_id)
    income = by_code["4-1000"]
    standard = tax_by_type["standard"]

    p1 = await _contact(company_id, "Small Buyer OÜ", ContactType.CUSTOMER, "10111111")
    await _post_invoice(company_id, p1, income, standard, net=_D("50.00"), issue_date=date(2026, 2, 5))

    async with AsyncSessionLocal() as session:
        listing = await generate_kmd_2027(
            session, company_id=company_id,
            period_start=_PERIOD_START, period_end=_PERIOD_END,
        )

    assert len(listing.rows) == 1
    row = listing.rows[0]
    assert row.kmdtyyp_code == "M_101"
    assert row.amount == _D("50.00")
    assert row.tax_rate == _D("0.24")
    assert row.partner_code == "10111111"
    assert not listing.errors


async def test_credit_note_is_a_signed_row() -> None:
    company_id = await _ee_company_with_controls()
    by_code, tax_by_type = await _accounts_and_codes(company_id)
    income = by_code["4-1000"]
    standard = tax_by_type["standard"]

    p1 = await _contact(company_id, "Refunded Buyer OÜ", ContactType.CUSTOMER, "10222222")
    await _post_invoice(company_id, p1, income, standard, net=_D("1000.00"), issue_date=date(2026, 2, 5))
    await _post_credit_note(company_id, p1, income, standard, net=_D("300.00"), issue_date=date(2026, 2, 20))

    async with AsyncSessionLocal() as session:
        listing = await generate_kmd_2027(
            session, company_id=company_id,
            period_start=_PERIOD_START, period_end=_PERIOD_END,
        )

    amounts = sorted(r.amount for r in listing.rows)
    assert amounts == [_D("-300.00"), _D("1000.00")]
    assert all(r.kmdtyyp_code == "M_101" for r in listing.rows)


async def test_domestic_input_bill_emits_o101_input_vat() -> None:
    company_id = await _ee_company_with_controls()
    by_code, tax_by_type = await _accounts_and_codes(company_id)
    expense = by_code["5-1000"]
    standard = tax_by_type["standard"]

    s1 = await _contact(company_id, "Supplier OÜ", ContactType.SUPPLIER, "10555555")
    await _post_bill(company_id, s1, expense, standard, net=_D("1000.00"), issue_date=date(2026, 2, 9))

    async with AsyncSessionLocal() as session:
        listing = await generate_kmd_2027(
            session, company_id=company_id,
            period_start=_PERIOD_START, period_end=_PERIOD_END,
        )

    assert len(listing.rows) == 1
    row = listing.rows[0]
    assert row.kmdtyyp_code == "O_101"
    assert row.amount == _D("240.00")  # 1000 * 24% input VAT


async def _seed_reconcile_scenario():
    company_id = await _ee_company_with_controls()
    by_code, tax_by_type = await _accounts_and_codes(company_id)
    income = by_code["4-1000"]
    expense = by_code["5-1000"]
    standard = tax_by_type["standard"]

    p1 = await _contact(company_id, "Buyer A OÜ", ContactType.CUSTOMER, "10111111")
    p2 = await _contact(company_id, "Buyer B OÜ", ContactType.CUSTOMER, "10222222")
    s1 = await _contact(company_id, "Supplier OÜ", ContactType.SUPPLIER, "10555555")
    await _post_invoice(company_id, p1, income, standard, net=_D("2400.00"), issue_date=date(2026, 2, 5))
    await _post_invoice(company_id, p2, income, standard, net=_D("600.00"), issue_date=date(2026, 2, 7))
    await _post_bill(company_id, s1, expense, standard, net=_D("1000.00"), issue_date=date(2026, 2, 9))
    return company_id


async def test_exported_rows_reconcile_to_box_engine() -> None:
    """End-to-end: the exported rows reconcile, by category, to the box vector
    the box engine derives from the same posted ledger.

    Uses ``_kmd_box_vector`` (the EMBEDDED EE-KMD box helper the existing EE
    golden tests trust) rather than the reference-DB path, so this assertion is
    independent of whether the test reference DB is seeded with EE KMD box
    definitions."""
    company_id = await _seed_reconcile_scenario()

    async with AsyncSessionLocal() as session:
        listing = await generate_kmd_2027(
            session, company_id=company_id,
            period_start=_PERIOD_START, period_end=_PERIOD_END,
        )
    box_amounts = await _kmd_box_vector(company_id, from_date=_PERIOD_START, to_date=_PERIOD_END)

    report = reconcile(listing.rows, box_amounts)
    assert not report.unclassified_rows
    supply = report.line_for("domestic_taxable_supply")
    assert supply.rows_total == _D("3000.00")
    assert supply.delta == _D("0")
    assert report.line_for("input_vat").rows_total == _D("240.00")
    assert report.reconciled


@pytest.mark.skipif(
    not _reference_db_configured,
    reason="REFERENCE_DATABASE_URL / REFERENCE_MIGRATION_DATABASE_URL not both configured",
)
async def test_reconcile_period_wrapper_runs() -> None:
    """The DB wrapper wires generator + box engine + pure reconcile without
    error and leaves nothing unclassified (a clean domestic period). Unlike
    ``test_exported_rows_reconcile_to_box_engine`` (which uses the embedded
    ``_kmd_box_vector`` helper and proves the reconcile logic), the wrapper
    drives ``generate_return`` down the REAL reference-DB read path — which needs
    both the read-side and migration-side reference DBs configured. The CI
    harness sets only the migration URL, so this skips there (same guard the AU
    reference-read test uses); it exercises fully when a read reference DB is set."""
    await load_seeds("EE")
    company_id = await _seed_reconcile_scenario()

    async with AsyncSessionLocal() as session:
        report = await reconcile_period(
            session, company_id=company_id,
            period_start=_PERIOD_START, period_end=_PERIOD_END,
        )

    assert not report.unclassified_rows
    assert report.line_for("domestic_taxable_supply").rows_total == _D("3000.00")
