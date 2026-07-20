"""The "functions with 0 modules" proof (jurisdiction-module Phase 0).

Design doc §3 (``jurisdiction-module-architecture-design.md``): with
ZERO jurisdiction modules bolted on — ``Company.jurisdiction`` set to
the reserved neutral sentinel ``"XX"`` — the engine must still be a
complete double-entry accountant. This test drives the record-creation
AND posting layers end-to-end:

* an XX company can CREATE and POST an invoice (Dr AR / Cr Income) and
  a bill (Dr Expense / Cr AP) without error;
* the posted journal lines carry the neutral zero-tax treatment
  snapshot (``NeutralTaxEngine`` — rate 0, tax 0, direction "none"),
  i.e. tax is simply NOT COMPUTED, not broken.

Before Phase 0 this crashed: ``journal._apply_tax_treatment`` called
``get_engine`` unconditionally and ``get_engine("XX")`` was a
``KeyError`` — a jurisdiction-less company could not post at all.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.bill import BillStatus
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.invoice import InvoiceStatus
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine
from saebooks.services import bills as bills_svc
from saebooks.services import invoices as invoices_svc
from saebooks.services.tax_engine import NEUTRAL_JURISDICTION

pytestmark = pytest.mark.postgres_only

_ISSUE = date(2026, 6, 1)
_DUE = date(2026, 6, 30)


async def _make_xx_company() -> dict[str, uuid.UUID]:
    """Throwaway XX-jurisdiction company with the minimal chart the
    invoice/bill posting paths resolve: AR 1-1200 + AP 2-1200 (the
    control-account fallback codes), one income + one expense account,
    and a customer + a supplier contact. Deliberately NO tax codes —
    zero modules means no per-jurisdiction tax_code seed exists."""
    company_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Company(
                id=company_id,
                name=f"Zero Modules {company_id.hex[:8]}",
                base_currency="AUD",
                fin_year_start_month=7,
                audit_mode="immutable",
                jurisdiction=NEUTRAL_JURISDICTION,
            )
        )
        await session.flush()

        accounts = {
            "ar": Account(
                company_id=company_id, code="1-1200",
                name="Trade Debtors", account_type=AccountType.ASSET,
            ),
            "ap": Account(
                company_id=company_id, code="2-1200",
                name="Trade Creditors", account_type=AccountType.LIABILITY,
            ),
            "income": Account(
                company_id=company_id, code="4-1000",
                name="Sales", account_type=AccountType.INCOME,
            ),
            "expense": Account(
                company_id=company_id, code="6-1000",
                name="Purchases", account_type=AccountType.EXPENSE,
            ),
        }
        session.add_all(accounts.values())

        customer = Contact(
            company_id=company_id, name="Neutral Customer",
            contact_type=ContactType.CUSTOMER,
        )
        supplier = Contact(
            company_id=company_id, name="Neutral Supplier",
            contact_type=ContactType.SUPPLIER,
        )
        session.add_all([customer, supplier])
        await session.commit()
        return {
            "company_id": company_id,
            "income": accounts["income"].id,
            "expense": accounts["expense"].id,
            "customer": customer.id,
            "supplier": supplier.id,
        }


async def _assert_neutral_treatments(journal_entry_id: uuid.UUID) -> None:
    """Every line of the posted entry carries the neutral snapshot —
    recorded, zero tax, invisible to any reporting bucket."""
    async with AsyncSessionLocal() as session:
        entry = await session.get(JournalEntry, journal_entry_id)
        assert entry is not None
        assert entry.status == EntryStatus.POSTED
        lines = (
            (
                await session.execute(
                    select(JournalLine).where(
                        JournalLine.entry_id == journal_entry_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert lines, "posted entry must have lines"
        for ln in lines:
            treatment = ln.tax_treatment
            assert treatment is not None
            assert treatment["jurisdiction"] == NEUTRAL_JURISDICTION
            assert Decimal(treatment["tax"]) == Decimal("0")
            assert treatment["direction"] == "none"
            assert treatment["reporting_type"] == "none"


async def test_xx_company_records_invoice_without_tax_compute() -> None:
    ids = await _make_xx_company()
    async with AsyncSessionLocal() as session:
        inv = await invoices_svc.create_draft(
            session,
            company_id=ids["company_id"],
            contact_id=ids["customer"],
            issue_date=_ISSUE,
            due_date=_DUE,
            lines=[
                {
                    "description": "Consulting — no jurisdiction tax",
                    "account_id": ids["income"],
                    "quantity": Decimal("2"),
                    "unit_price": Decimal("500"),
                    "discount_pct": Decimal("0"),
                },
            ],
        )
    assert inv.total == Decimal("1000.00")
    assert inv.tax_total == Decimal("0.00")

    async with AsyncSessionLocal() as session:
        posted = await invoices_svc.post_invoice(
            session, inv.id, posted_by="zero-modules-test"
        )
    assert posted.status == InvoiceStatus.POSTED
    assert posted.journal_entry_id is not None
    await _assert_neutral_treatments(posted.journal_entry_id)


async def test_xx_company_records_bill_without_tax_compute() -> None:
    ids = await _make_xx_company()
    async with AsyncSessionLocal() as session:
        bill = await bills_svc.create_draft(
            session,
            company_id=ids["company_id"],
            contact_id=ids["supplier"],
            issue_date=_ISSUE,
            due_date=_DUE,
            lines=[
                {
                    "description": "Supplies — no jurisdiction tax",
                    "account_id": ids["expense"],
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("750"),
                    "discount_pct": Decimal("0"),
                },
            ],
        )
    assert bill.total == Decimal("750.00")
    assert bill.tax_total == Decimal("0.00")

    async with AsyncSessionLocal() as session:
        posted = await bills_svc.post_bill(
            session, bill.id, posted_by="zero-modules-test"
        )
    assert posted.status == BillStatus.POSTED
    assert posted.journal_entry_id is not None
    await _assert_neutral_treatments(posted.journal_entry_id)
