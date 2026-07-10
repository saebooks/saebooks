"""Targeted tests for ``saebooks.services.expenses`` — critic-round-4 fix.

Not a full lifecycle mirror of ``test_bills.py`` (no existing service-level
test file covered ``services/expenses.py`` before this fix); scoped to the
one gap this round closes: ``post_expense`` must refuse an EU-acquisition
reverse-charge tax code rather than silently overstate the payment-account
credit by the self-assessed VAT and never book the output-side liability.
See ``services.expenses._reject_unsupported_reverse_charge``.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.expense import Expense, ExpenseStatus
from saebooks.models.tax_code import TaxCode
from saebooks.services import expenses as svc

pytestmark = pytest.mark.postgres_only


@pytest.mark.asyncio
async def test_post_expense_rejects_reverse_charge_eu_acquisition() -> None:
    """Same gap and remedy as ``test_bills.py``'s equivalent guard test —
    see ``services.expenses._reject_unsupported_reverse_charge``."""
    company_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        company = Company(
            id=company_id,
            name=f"RC Guard Expense Test {company_id.hex[:8]}",
            base_currency="EUR",
            fin_year_start_month=1,
            audit_mode="immutable",
            jurisdiction="EE",
        )
        session.add(company)
        await session.flush()

        expense_acct = Account(
            company_id=company_id,
            code="6-1000",
            name="Purchases",
            account_type=AccountType.EXPENSE,
        )
        bank_acct = Account(
            company_id=company_id,
            code="1-1110",
            name="Bank",
            account_type=AccountType.ASSET,
        )
        session.add_all([expense_acct, bank_acct])
        await session.flush()

        rc_tc = TaxCode(
            company_id=company_id,
            code="RC-EUACQ",
            name="EE reverse charge — EU acquisition of goods (24%)",
            rate=Decimal("24.000"),
            tax_system="VAT",
            jurisdiction="EE",
            reporting_type="rc_eu_acq_goods",
        )
        session.add(rc_tc)

        contact = Contact(
            company_id=company_id,
            name="EU Supplier OU",
            contact_type=ContactType.SUPPLIER,
            email="eu-supplier@example.com",
        )
        session.add(contact)
        await session.commit()
        await session.refresh(expense_acct)
        await session.refresh(bank_acct)
        await session.refresh(rc_tc)
        await session.refresh(contact)

    async with AsyncSessionLocal() as session:
        expense = await svc.create_draft(
            session,
            company_id=company_id,
            payment_account_id=bank_acct.id,
            expense_date=date(2026, 6, 1),
            contact_id=contact.id,
            lines=[
                {
                    "description": "EU acquisition of goods",
                    "account_id": expense_acct.id,
                    "tax_code_id": rc_tc.id,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("4000"),
                    "discount_pct": Decimal("0"),
                },
            ],
        )

    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.ExpenseError, match="reverse-charge"):
            await svc.post_expense(session, expense.id, posted_by="test")

    async with AsyncSessionLocal() as session:
        refreshed = await session.get(Expense, expense.id)
        assert refreshed is not None
        assert refreshed.status == ExpenseStatus.DRAFT
        assert refreshed.journal_entry_id is None
