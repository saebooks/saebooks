"""Cross-company FK validation tests (P0-1 + P0-2 from saebooks-real-p0s.md).

RLS only filters by ``tenant_id``. Within a single tenant a user
holding two companies must not be able to mix tenant-scoped FKs
across them on writes (tax_code from company B inside an invoice in
company A, etc).

Covered write paths:

* invoices.create_draft        — line.tax_code_id, line.account_id, contact_id
* bills.create_draft           — line.tax_code_id, line.account_id, contact_id
* credit_notes.create_draft    — line.tax_code_id, line.account_id, contact_id
* journal_entries.create       — line.account_id, line.tax_code_id

Each test creates a sibling company in the same default tenant,
seeds a single foreign FK there, then attempts a write into the
primary company referencing that foreign id — expects the service
to raise its own ValueError subclass with an opaque "<model> <uuid>
not found" message (no leak of the foreign company id).
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.tax_code import TaxCode
from saebooks.services import bills as bills_svc
from saebooks.services import credit_notes as cn_svc
from saebooks.services import invoices as inv_svc
from saebooks.services import journal_entries as je_svc

_DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
async def sibling_company() -> AsyncGenerator[
    tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID], None
]:
    """Create company B in the same tenant + a tax_code, account, contact in B.

    Yields (company_a_id, company_b_id, tc_b_id, acct_b_id, contact_b_id).
    company_a is the existing dev-seeded primary company.
    """
    tag = uuid.uuid4().hex[:8]
    async with AsyncSessionLocal() as session:
        company_a = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company_a is not None
        company_b = Company(
            name=f"SiblingCo-{tag}",
            base_currency="AUD",
            tenant_id=_DEFAULT_TENANT,
        )
        session.add(company_b)
        await session.flush()

        tc_b = TaxCode(
            company_id=company_b.id,
            tenant_id=_DEFAULT_TENANT,
            code=f"GST-{tag}",
            name=f"Sibling GST {tag}",
            rate=Decimal("10"),
            tax_system="GST",
            reporting_type="taxable",
        )
        acct_b = Account(
            company_id=company_b.id,
            tenant_id=_DEFAULT_TENANT,
            code=f"4-{tag[:4]}",
            name=f"Sibling Income {tag}",
            account_type=AccountType.INCOME,
        )
        contact_b = Contact(
            company_id=company_b.id,
            tenant_id=_DEFAULT_TENANT,
            name=f"Sibling Customer {tag}",
            contact_type=ContactType.CUSTOMER,
        )
        session.add_all([tc_b, acct_b, contact_b])
        await session.commit()
        ids = (company_a.id, company_b.id, tc_b.id, acct_b.id, contact_b.id)

    yield ids

    async with AsyncSessionLocal() as session:
        for model, mid in (
            (Contact, ids[4]),
            (TaxCode, ids[2]),
            (Account, ids[3]),
        ):
            row = await session.get(model, mid)
            if row is not None:
                await session.delete(row)
        company_b = await session.get(Company, ids[1])
        if company_b is not None:
            await session.delete(company_b)
        await session.commit()


async def _primary_account(company_id: uuid.UUID, type_: AccountType) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        acct = (
            await session.execute(
                select(Account)
                .where(
                    Account.company_id == company_id,
                    Account.account_type == type_,
                    Account.archived_at.is_(None),
                )
                .limit(1)
            )
        ).scalar_one()
        return acct.id


async def _primary_contact(company_id: uuid.UUID, type_: ContactType) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        ct = (
            await session.execute(
                select(Contact)
                .where(
                    Contact.company_id == company_id,
                    Contact.contact_type == type_,
                    Contact.archived_at.is_(None),
                )
                .limit(1)
            )
        ).scalar_one()
        return ct.id


@pytest.mark.asyncio
async def test_invoice_rejects_sibling_tax_code(sibling_company: tuple) -> None:
    cid_a, _cid_b, tc_b, _acct_b, _ct_b = sibling_company
    acct_a = await _primary_account(cid_a, AccountType.INCOME)
    contact_a = await _primary_contact(cid_a, ContactType.CUSTOMER)

    async with AsyncSessionLocal() as session:
        with pytest.raises(inv_svc.InvoiceError, match="tax_code .* not found"):
            await inv_svc.create_draft(
                session,
                company_id=cid_a,
                contact_id=contact_a,
                issue_date=date(2026, 4, 29),
                due_date=date(2026, 5, 29),
                lines=[
                    {
                        "description": "x",
                        "account_id": acct_a,
                        "tax_code_id": tc_b,
                        "quantity": Decimal("1"),
                        "unit_price": Decimal("100"),
                        "discount_pct": Decimal("0"),
                    }
                ],
            )


@pytest.mark.asyncio
async def test_invoice_rejects_sibling_account(sibling_company: tuple) -> None:
    cid_a, _cid_b, _tc_b, acct_b, _ct_b = sibling_company
    contact_a = await _primary_contact(cid_a, ContactType.CUSTOMER)

    async with AsyncSessionLocal() as session:
        with pytest.raises(inv_svc.InvoiceError, match="account .* not found"):
            await inv_svc.create_draft(
                session,
                company_id=cid_a,
                contact_id=contact_a,
                issue_date=date(2026, 4, 29),
                due_date=date(2026, 5, 29),
                lines=[
                    {
                        "description": "x",
                        "account_id": acct_b,
                        "quantity": Decimal("1"),
                        "unit_price": Decimal("100"),
                        "discount_pct": Decimal("0"),
                    }
                ],
            )


@pytest.mark.asyncio
async def test_invoice_rejects_sibling_contact(sibling_company: tuple) -> None:
    cid_a, _cid_b, _tc_b, _acct_b, ct_b = sibling_company
    acct_a = await _primary_account(cid_a, AccountType.INCOME)

    async with AsyncSessionLocal() as session:
        with pytest.raises(inv_svc.InvoiceError, match="contact .* not found"):
            await inv_svc.create_draft(
                session,
                company_id=cid_a,
                contact_id=ct_b,
                issue_date=date(2026, 4, 29),
                due_date=date(2026, 5, 29),
                lines=[
                    {
                        "description": "x",
                        "account_id": acct_a,
                        "quantity": Decimal("1"),
                        "unit_price": Decimal("100"),
                        "discount_pct": Decimal("0"),
                    }
                ],
            )


@pytest.mark.asyncio
async def test_bill_rejects_sibling_tax_code(sibling_company: tuple) -> None:
    cid_a, _cid_b, tc_b, _acct_b, _ct_b = sibling_company
    acct_a = await _primary_account(cid_a, AccountType.EXPENSE)
    contact_a = await _primary_contact(cid_a, ContactType.SUPPLIER)

    async with AsyncSessionLocal() as session:
        with pytest.raises(bills_svc.BillError, match="tax_code .* not found"):
            await bills_svc.create_draft(
                session,
                company_id=cid_a,
                contact_id=contact_a,
                issue_date=date(2026, 4, 29),
                due_date=date(2026, 5, 29),
                lines=[
                    {
                        "description": "x",
                        "account_id": acct_a,
                        "tax_code_id": tc_b,
                        "quantity": Decimal("1"),
                        "unit_price": Decimal("50"),
                        "discount_pct": Decimal("0"),
                    }
                ],
            )


@pytest.mark.asyncio
async def test_bill_rejects_sibling_account(sibling_company: tuple) -> None:
    cid_a, _cid_b, _tc_b, acct_b, _ct_b = sibling_company
    contact_a = await _primary_contact(cid_a, ContactType.SUPPLIER)

    async with AsyncSessionLocal() as session:
        with pytest.raises(bills_svc.BillError, match="account .* not found"):
            await bills_svc.create_draft(
                session,
                company_id=cid_a,
                contact_id=contact_a,
                issue_date=date(2026, 4, 29),
                due_date=date(2026, 5, 29),
                lines=[
                    {
                        "description": "x",
                        "account_id": acct_b,
                        "quantity": Decimal("1"),
                        "unit_price": Decimal("50"),
                        "discount_pct": Decimal("0"),
                    }
                ],
            )


@pytest.mark.asyncio
async def test_credit_note_rejects_sibling_tax_code(sibling_company: tuple) -> None:
    cid_a, _cid_b, tc_b, _acct_b, _ct_b = sibling_company
    acct_a = await _primary_account(cid_a, AccountType.INCOME)
    contact_a = await _primary_contact(cid_a, ContactType.CUSTOMER)

    async with AsyncSessionLocal() as session:
        with pytest.raises(cn_svc.CreditNoteError, match="tax_code .* not found"):
            await cn_svc.create_draft(
                session,
                company_id=cid_a,
                contact_id=contact_a,
                issue_date=date(2026, 4, 29),
                lines=[
                    {
                        "description": "x",
                        "account_id": acct_a,
                        "tax_code_id": tc_b,
                        "quantity": Decimal("1"),
                        "unit_price": Decimal("100"),
                        "discount_pct": Decimal("0"),
                    }
                ],
            )


@pytest.mark.asyncio
async def test_journal_entry_rejects_sibling_account(sibling_company: tuple) -> None:
    cid_a, _cid_b, _tc_b, acct_b, _ct_b = sibling_company
    acct_a = await _primary_account(cid_a, AccountType.INCOME)

    async with AsyncSessionLocal() as session:
        with pytest.raises(je_svc.JournalEntryError, match="account .* not found"):
            await je_svc.create(
                session,
                company_id=cid_a,
                tenant_id=_DEFAULT_TENANT,
                actor="test",
                entry_date=date(2026, 4, 29),
                narration="cross-company JE",
                lines=[
                    {
                        "account_id": acct_b,
                        "debit": Decimal("100"),
                        "credit": Decimal("0"),
                    },
                    {
                        "account_id": acct_a,
                        "debit": Decimal("0"),
                        "credit": Decimal("100"),
                    },
                ],
            )


@pytest.mark.asyncio
async def test_journal_entry_rejects_sibling_tax_code(sibling_company: tuple) -> None:
    cid_a, _cid_b, tc_b, _acct_b, _ct_b = sibling_company
    acct_a_income = await _primary_account(cid_a, AccountType.INCOME)
    acct_a_expense = await _primary_account(cid_a, AccountType.EXPENSE)

    async with AsyncSessionLocal() as session:
        with pytest.raises(je_svc.JournalEntryError, match="tax_code .* not found"):
            await je_svc.create(
                session,
                company_id=cid_a,
                tenant_id=_DEFAULT_TENANT,
                actor="test",
                entry_date=date(2026, 4, 29),
                narration="cross-company tax code on JE",
                lines=[
                    {
                        "account_id": acct_a_expense,
                        "tax_code_id": tc_b,
                        "debit": Decimal("100"),
                        "credit": Decimal("0"),
                    },
                    {
                        "account_id": acct_a_income,
                        "debit": Decimal("0"),
                        "credit": Decimal("100"),
                    },
                ],
            )
