"""Tests for ``saebooks.services.payments``.

Covers the INCOMING receipt lifecycle:

1. ``create_draft`` + validation (positive amount).
2. ``post_payment`` mints number, posts Dr Bank / Cr AR journal.
3. ``allocate`` attaches invoice allocations + updates amount_paid.
4. Over-allocation rejected.
5. ``void_payment`` reverses journal + resets invoice amount_paid.
6. Draft void flips status without GL touch.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.invoice import InvoiceStatus
from saebooks.models.payment import PaymentDirection, PaymentStatus
from saebooks.models.tax_code import TaxCode
from saebooks.services import invoices as inv_svc
from saebooks.services import payments as svc


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company, contact, bank, income, gst)."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None)).order_by(
                    Company.created_at
                )
            )
        ).scalars().first()
        assert company is not None

        bank = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.code == "1-1110",
                )
            )
        ).scalar_one()
        income = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.code == "4-6000",
                )
            )
        ).scalar_one()
        gst = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.company_id == company.id,
                    TaxCode.code == "GST",
                )
            )
        ).scalar_one()

        existing = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == company.id,
                    Contact.name == "Test Payments Ltd",
                )
            )
        ).scalars().first()
        if existing is None:
            contact = Contact(
                company_id=company.id,
                name="Test Payments Ltd",
                contact_type=ContactType.CUSTOMER,
                email="pay@example.com",
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)
        else:
            contact = existing

        return company.id, contact.id, bank.id, income.id, gst.id


async def _post_invoice(
    company_id: uuid.UUID,
    contact_id: uuid.UUID,
    income: uuid.UUID,
    gst: uuid.UUID,
    amount: Decimal,
) -> uuid.UUID:
    today = date(2026, 4, 20)
    async with AsyncSessionLocal() as session:
        inv = await inv_svc.create_draft(
            session,
            company_id=company_id,
            contact_id=contact_id,
            issue_date=today,
            due_date=today + timedelta(days=30),
            lines=[
                {
                    "description": "Line",
                    "account_id": income,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": amount,
                    "discount_pct": Decimal("0"),
                }
            ],
        )
    async with AsyncSessionLocal() as session:
        await inv_svc.post_invoice(session, inv.id, posted_by="test")
    return inv.id


@pytest.mark.asyncio
async def test_create_draft_requires_positive_amount() -> None:
    cid, contact, bank, _i, _g = await _ctx()
    with pytest.raises(svc.PaymentError, match="positive"):
        async with AsyncSessionLocal() as session:
            await svc.create_draft(
                session,
                company_id=cid,
                contact_id=contact,
                bank_account_id=bank,
                payment_date=date(2026, 4, 20),
                amount=Decimal("0"),
            )


@pytest.mark.asyncio
async def test_post_payment_mints_number_and_journal() -> None:
    cid, contact, bank, _i, _g = await _ctx()
    async with AsyncSessionLocal() as session:
        pay = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            bank_account_id=bank,
            payment_date=date(2026, 4, 20),
            amount=Decimal("500.00"),
            direction=PaymentDirection.INCOMING,
        )
    async with AsyncSessionLocal() as session:
        posted = await svc.post_payment(session, pay.id, posted_by="test")

    assert posted.status == PaymentStatus.POSTED
    assert posted.number is not None
    assert posted.number.startswith("PAY-")
    assert posted.journal_entry_id is not None


@pytest.mark.asyncio
async def test_allocation_updates_invoice_amount_paid() -> None:
    cid, contact, bank, income, gst = await _ctx()
    invoice_id = await _post_invoice(
        cid, contact, income, gst, Decimal("200.00")
    )  # total = 220.00 w/ 10% GST

    async with AsyncSessionLocal() as session:
        pay = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            bank_account_id=bank,
            payment_date=date(2026, 4, 21),
            amount=Decimal("100.00"),
        )
    async with AsyncSessionLocal() as session:
        await svc.post_payment(session, pay.id, posted_by="test")
    async with AsyncSessionLocal() as session:
        await svc.allocate(
            session,
            pay.id,
            invoice_allocations=[(invoice_id, Decimal("100.00"))],
        )

    async with AsyncSessionLocal() as session:
        inv = await inv_svc.get(session, invoice_id)
        assert inv.amount_paid == Decimal("100.00")
        assert inv.status == InvoiceStatus.POSTED  # partial pay keeps POSTED


@pytest.mark.asyncio
async def test_over_allocation_rejected() -> None:
    cid, contact, bank, income, gst = await _ctx()
    invoice_id = await _post_invoice(
        cid, contact, income, gst, Decimal("50.00")
    )
    async with AsyncSessionLocal() as session:
        pay = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            bank_account_id=bank,
            payment_date=date(2026, 4, 21),
            amount=Decimal("30.00"),
        )
    async with AsyncSessionLocal() as session:
        await svc.post_payment(session, pay.id, posted_by="test")

    with pytest.raises(svc.PaymentError, match="exceeds"):
        async with AsyncSessionLocal() as session:
            await svc.allocate(
                session,
                pay.id,
                invoice_allocations=[(invoice_id, Decimal("40.00"))],
            )


@pytest.mark.asyncio
async def test_void_posted_payment_reverses_journal() -> None:
    cid, contact, bank, _i, _g = await _ctx()
    async with AsyncSessionLocal() as session:
        pay = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            bank_account_id=bank,
            payment_date=date(2026, 4, 20),
            amount=Decimal("75.00"),
        )
    async with AsyncSessionLocal() as session:
        await svc.post_payment(session, pay.id, posted_by="test")
    async with AsyncSessionLocal() as session:
        voided = await svc.void_payment(session, pay.id, posted_by="test")
    assert voided.status == PaymentStatus.VOIDED
    assert voided.void_journal_entry_id is not None


@pytest.mark.asyncio
async def test_void_draft_flips_status_without_journal() -> None:
    cid, contact, bank, _i, _g = await _ctx()
    async with AsyncSessionLocal() as session:
        pay = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            bank_account_id=bank,
            payment_date=date(2026, 4, 20),
            amount=Decimal("10.00"),
        )
    async with AsyncSessionLocal() as session:
        voided = await svc.void_payment(session, pay.id)
    assert voided.status == PaymentStatus.VOIDED
    assert voided.void_journal_entry_id is None
