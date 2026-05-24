"""Regression tests for migration 0102 — payment_allocations DB guards.

Pins the audit's CRITICAL #2 / #3: a write that bypasses
``saebooks.services.payments`` cannot

* leave an allocation row pointing at zero or two+ documents (XOR
  CHECK), or
* push the cumulative allocation against any one document past its
  total (CONSTRAINT TRIGGER).

Each test goes around the service layer with raw SQL INSERTs — the
service's own assertions are not what we're checking.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from saebooks.db import AsyncSessionLocal, Base
from saebooks.models.account import Account
from saebooks.models.bill import Bill
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.document_counter import DocumentCounter
from saebooks.models.invoice import Invoice
from saebooks.models.payment import Payment, PaymentDirection
from saebooks.models.tax_code import TaxCode
from saebooks.services import invoices as inv_svc
from saebooks.services import payments as pay_svc
pytestmark = pytest.mark.postgres_only


_COUNTER_PREFIXES = {
    "invoice": "INV-",
    "bill": "BILL-",
    "payment": "PAY-",
    "credit_note": "CN-",
}


async def _fast_forward_counter(kind: str, model_cls: type[Base]) -> None:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None
        numbers = (
            await session.execute(
                select(model_cls.number).where(  # type: ignore[attr-defined]
                    model_cls.company_id == company.id,  # type: ignore[attr-defined]
                    model_cls.number.isnot(None),  # type: ignore[attr-defined]
                )
            )
        ).scalars().all()
        max_suffix = 0
        for n in numbers:
            try:
                max_suffix = max(max_suffix, int(str(n).rsplit("-", 1)[-1]))
            except ValueError:
                continue
        counter = (
            await session.execute(
                select(DocumentCounter).where(
                    DocumentCounter.company_id == company.id,
                    DocumentCounter.kind == kind,
                )
            )
        ).scalar_one_or_none()
        if counter is None:
            counter = DocumentCounter(
                company_id=company.id,
                kind=kind,
                prefix=_COUNTER_PREFIXES[kind],
                pad_width=6,
                next_value=max_suffix + 1,
            )
            session.add(counter)
        elif counter.next_value <= max_suffix:
            counter.next_value = max_suffix + 1
        await session.commit()


@pytest.fixture(autouse=True, scope="module")
async def _prep_counters() -> AsyncGenerator[None, None]:
    await _fast_forward_counter("invoice", Invoice)
    await _fast_forward_counter("payment", Payment)
    await _fast_forward_counter("bill", Bill)
    yield


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company_id, contact_id, bank_id, income_id, gst_id)."""
    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert co is not None
        bank = (
            await session.execute(
                select(Account).where(
                    Account.company_id == co.id, Account.code == "1-1110"
                )
            )
        ).scalar_one()
        income = (
            await session.execute(
                select(Account).where(
                    Account.company_id == co.id, Account.code == "4-6000"
                )
            )
        ).scalar_one()
        gst = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.company_id == co.id, TaxCode.code == "GST"
                )
            )
        ).scalar_one()
        existing = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == co.id,
                    Contact.name == "Allocation Trigger Co",
                )
            )
        ).scalars().first()
        if existing is None:
            contact = Contact(
                company_id=co.id,
                name="Allocation Trigger Co",
                contact_type=ContactType.CUSTOMER,
                email="alloc-trg@example.com",
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)
        else:
            contact = existing
        return co.id, contact.id, bank.id, income.id, gst.id


async def _posted_invoice(
    company_id: uuid.UUID,
    contact_id: uuid.UUID,
    income: uuid.UUID,
    gst: uuid.UUID,
    *,
    unit_price: Decimal,
) -> tuple[uuid.UUID, Decimal]:
    """Post a single-line invoice; return (invoice_id, total)."""
    today = date(2026, 5, 10)
    async with AsyncSessionLocal() as session:
        inv = await inv_svc.create_draft(
            session,
            company_id=company_id,
            contact_id=contact_id,
            issue_date=today,
            due_date=today + timedelta(days=30),
            lines=[
                {
                    "description": "Trigger test",
                    "account_id": income,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": unit_price,
                    "discount_pct": Decimal("0"),
                }
            ],
        )
    async with AsyncSessionLocal() as session:
        await inv_svc.post_invoice(session, inv.id, posted_by="test")
    async with AsyncSessionLocal() as session:
        loaded = await inv_svc.get(session, inv.id)
        return inv.id, loaded.total


async def _posted_payment(
    company_id: uuid.UUID,
    contact_id: uuid.UUID,
    bank_id: uuid.UUID,
    *,
    amount: Decimal,
) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        pay = await pay_svc.create_draft(
            session,
            company_id=company_id,
            contact_id=contact_id,
            bank_account_id=bank_id,
            payment_date=date(2026, 5, 10),
            amount=amount,
            direction=PaymentDirection.INCOMING,
        )
    async with AsyncSessionLocal() as session:
        await pay_svc.post_payment(session, pay.id, posted_by="test")
    return pay.id


async def _insert_allocation_raw(
    payment_id: uuid.UUID,
    *,
    invoice_id: uuid.UUID | None = None,
    credit_note_id: uuid.UUID | None = None,
    bill_id: uuid.UUID | None = None,
    amount: Decimal,
    alloc_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Bypass services.payments.allocate and INSERT directly."""
    aid = alloc_id or uuid.uuid4()
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                INSERT INTO payment_allocations
                    (id, payment_id, invoice_id, credit_note_id, bill_id, amount)
                VALUES
                    (:id, :pid, :inv, :cn, :bill, :amt)
                """
            ),
            {
                "id": aid,
                "pid": payment_id,
                "inv": invoice_id,
                "cn": credit_note_id,
                "bill": bill_id,
                "amt": amount,
            },
        )
        await session.commit()
    return aid


# ---------------------------------------------------------------------- #
# 1. XOR CHECK                                                           #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_xor_rejects_two_targets() -> None:
    cid, contact, bank, income, gst = await _ctx()
    inv_id, total = await _posted_invoice(
        cid, contact, income, gst, unit_price=Decimal("100.00")
    )
    pay_id = await _posted_payment(
        cid, contact, bank, amount=Decimal("50.00")
    )

    # Forge a fake bill_id — only the FK has to point at *something*
    # that exists if the row is going to make it in. Easier: skip the
    # FK by using a real bill row would require seeding an AP account
    # tree; instead, leverage the fact that bill_id is only checked by
    # FK on COMMIT, so we can use a uuid that doesn't exist and the
    # CHECK should fire FIRST. Actually the CHECK fires immediately on
    # the INSERT (it's a row-level CHECK), so the FK doesn't even get
    # validated. Use a sentinel uuid.
    fake_bill_id = uuid.uuid4()
    with pytest.raises(IntegrityError):
        await _insert_allocation_raw(
            pay_id,
            invoice_id=inv_id,
            bill_id=fake_bill_id,
            amount=Decimal("50.00"),
        )
    # `total` is unused beyond establishing the invoice exists.
    _ = total


@pytest.mark.asyncio
async def test_xor_rejects_zero_targets() -> None:
    cid, contact, bank, _i, _g = await _ctx()
    pay_id = await _posted_payment(
        cid, contact, bank, amount=Decimal("25.00")
    )
    with pytest.raises(IntegrityError):
        await _insert_allocation_raw(
            pay_id,
            amount=Decimal("25.00"),
        )


# ---------------------------------------------------------------------- #
# 2. Cumulative allocation cap                                           #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cap_rejects_over_allocation() -> None:
    cid, contact, bank, income, gst = await _ctx()
    inv_id, total = await _posted_invoice(
        cid, contact, income, gst, unit_price=Decimal("100.00")
    )
    pay_id = await _posted_payment(
        cid, contact, bank, amount=total + Decimal("50.00")
    )
    with pytest.raises(IntegrityError, match="exceeds total"):
        await _insert_allocation_raw(
            pay_id,
            invoice_id=inv_id,
            amount=total + Decimal("0.01"),
        )


@pytest.mark.asyncio
async def test_cap_allows_exactly_total() -> None:
    cid, contact, bank, income, gst = await _ctx()
    inv_id, total = await _posted_invoice(
        cid, contact, income, gst, unit_price=Decimal("60.00")
    )
    pay_id = await _posted_payment(cid, contact, bank, amount=total)
    aid = await _insert_allocation_raw(
        pay_id, invoice_id=inv_id, amount=total
    )

    async with AsyncSessionLocal() as s:
        await s.execute(
            text("DELETE FROM payment_allocations WHERE id = :id"),
            {"id": aid},
        )
        await s.commit()


@pytest.mark.asyncio
async def test_cap_sums_across_multiple_rows() -> None:
    """Two rows each ≤ total but together over — second INSERT rejected."""
    cid, contact, bank, income, gst = await _ctx()
    inv_id, total = await _posted_invoice(
        cid, contact, income, gst, unit_price=Decimal("80.00")
    )
    pay_id = await _posted_payment(
        cid, contact, bank, amount=total * 2
    )
    half = (total / 2).quantize(Decimal("0.01"))
    aid1 = await _insert_allocation_raw(
        pay_id, invoice_id=inv_id, amount=half
    )

    over = total - half + Decimal("0.01")
    with pytest.raises(IntegrityError, match="exceeds total"):
        await _insert_allocation_raw(
            pay_id, invoice_id=inv_id, amount=over
        )

    async with AsyncSessionLocal() as s:
        await s.execute(
            text("DELETE FROM payment_allocations WHERE id = :id"),
            {"id": aid1},
        )
        await s.commit()


# ---------------------------------------------------------------------- #
# 3. UPDATE re-pointing must validate the old doc too                    #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_update_repoint_revalidates_new_doc() -> None:
    """Repoint an allocation onto a smaller invoice that can't absorb it."""
    cid, contact, bank, income, gst = await _ctx()
    big_id, big_total = await _posted_invoice(
        cid, contact, income, gst, unit_price=Decimal("200.00")
    )
    small_id, small_total = await _posted_invoice(
        cid, contact, income, gst, unit_price=Decimal("10.00")
    )
    assert small_total < big_total
    pay_id = await _posted_payment(cid, contact, bank, amount=big_total)
    aid = await _insert_allocation_raw(
        pay_id, invoice_id=big_id, amount=big_total
    )

    with pytest.raises(IntegrityError, match="exceeds total"):
        async with AsyncSessionLocal() as s:
            await s.execute(
                text(
                    "UPDATE payment_allocations SET invoice_id = :new "
                    "WHERE id = :id"
                ),
                {"new": small_id, "id": aid},
            )
            await s.commit()

    async with AsyncSessionLocal() as s:
        await s.execute(
            text("DELETE FROM payment_allocations WHERE id = :id"),
            {"id": aid},
        )
        await s.commit()
