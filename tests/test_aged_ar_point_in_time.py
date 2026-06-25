"""Point-in-time correctness of ``reports.aged_ar`` / ``aged_ap``.

The aged subledger reports must be genuinely AS-OF-DATE: a credit note or
payment dated *after* the report's as-of date must NOT reduce the
outstanding balance. Previously the report used the scalar
``Invoice.amount_paid`` (current settled total, date-blind), so a future
credit note wrongly zeroed an invoice in a historical report.

These tests are fully self-contained: they create and select their own
company (with its own CoA + GST tax code), so they do not depend on seed
ordering or any pre-existing data.

Covers:
  * invoice + POSTED CN dated AFTER cutoff → outstanding as-of cutoff
    (before CN date); cleared as-of >= CN date.
  * payment dated after cutoff → invoice still outstanding as-of cutoff.
  * CN / payment dated on or before cutoff → reduces outstanding
    as-of cutoff (no regression).
  * AP: payment dated after cutoff → bill still outstanding as-of cutoff.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.document_counter import DocumentCounter
from saebooks.models.payment import PaymentDirection
from saebooks.seed.load_au_coa import _load_accounts, ensure_tax_codes
from saebooks.services import bills as bill_svc
from saebooks.services import credit_notes as cn_svc
from saebooks.services import invoices as inv_svc
from saebooks.services import payments as pay_svc
from saebooks.services import reports as svc

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _make_company() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create a self-contained company with CoA + GST + counters.

    Returns (company, contact, income, expense, bank, gst_tax_code).
    """
    from saebooks.models.tax_code import TaxCode
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        company = Company(
            tenant_id=_TENANT,
            name=f"PIT Aged Test {uuid.uuid4().hex[:8]}",
            base_currency="AUD",
            fin_year_start_month=7,
        )
        session.add(company)
        await session.commit()
        await session.refresh(company)

        await ensure_tax_codes(session, company.id)
        await _load_accounts(session, company)
        await session.commit()

        # Document counters so invoice/bill/payment/credit_note numbers mint.
        for kind, prefix in (
            ("invoice", "INV-"),
            ("bill", "BILL-"),
            ("payment", "PAY-"),
            ("credit_note", "CN-"),
        ):
            session.add(
                DocumentCounter(
                    company_id=company.id,
                    kind=kind,
                    prefix=prefix,
                    pad_width=6,
                    next_value=1,
                )
            )

        contact = Contact(
            company_id=company.id,
            name="PIT Debtor",
            contact_type=ContactType.CUSTOMER,
            email="pit@example.com",
        )
        session.add(contact)
        await session.commit()
        await session.refresh(contact)

        income = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id, Account.code == "4-6000"
                )
            )
        ).scalar_one()
        expense = (
            await session.execute(
                select(Account)
                .where(
                    Account.company_id == company.id,
                    Account.account_type == AccountType.EXPENSE,
                    Account.archived_at.is_(None),
                )
                .order_by(Account.code)
            )
        ).scalars().first()
        assert expense is not None
        bank = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id, Account.code == "1-1110"
                )
            )
        ).scalar_one()
        gst = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.company_id == company.id, TaxCode.code == "GST"
                )
            )
        ).scalar_one()
        return company.id, contact.id, income.id, expense.id, bank.id, gst.id


async def _post_invoice(
    company: uuid.UUID, contact: uuid.UUID, income: uuid.UUID, gst: uuid.UUID,
    amount: Decimal, *, issue: date,
) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        inv = await inv_svc.create_draft(
            session,
            company_id=company,
            contact_id=contact,
            issue_date=issue,
            due_date=issue + timedelta(days=30),
            lines=[{
                "description": "Line", "account_id": income, "tax_code_id": gst,
                "quantity": Decimal("1"), "unit_price": amount,
                "discount_pct": Decimal("0"),
            }],
        )
    async with AsyncSessionLocal() as session:
        await inv_svc.post_invoice(session, inv.id, posted_by="test")
    return inv.id


async def _post_bill(
    company: uuid.UUID, contact: uuid.UUID, expense: uuid.UUID, gst: uuid.UUID,
    amount: Decimal, *, issue: date,
) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        bill = await bill_svc.create_draft(
            session,
            company_id=company,
            contact_id=contact,
            issue_date=issue,
            due_date=issue + timedelta(days=30),
            lines=[{
                "description": "Line", "account_id": expense, "tax_code_id": gst,
                "quantity": Decimal("1"), "unit_price": amount,
                "discount_pct": Decimal("0"),
            }],
        )
    async with AsyncSessionLocal() as session:
        await bill_svc.post_bill(session, bill.id, posted_by="test")
    return bill.id


async def _post_cn(
    company: uuid.UUID, contact: uuid.UUID, expense: uuid.UUID, gst: uuid.UUID,
    amount: Decimal, *, issue: date, original_invoice_id: uuid.UUID,
) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        cn = await cn_svc.create_draft(
            session,
            company_id=company,
            contact_id=contact,
            issue_date=issue,
            original_invoice_id=original_invoice_id,
            lines=[{
                "description": "Write-off", "account_id": expense,
                "tax_code_id": gst, "quantity": Decimal("1"),
                "unit_price": amount, "discount_pct": Decimal("0"),
            }],
        )
    async with AsyncSessionLocal() as session:
        posted = await cn_svc.post_credit_note(session, cn.id, posted_by="test")
    return posted.id


async def _pay_invoice(
    company: uuid.UUID, contact: uuid.UUID, bank: uuid.UUID,
    invoice_id: uuid.UUID, amount: Decimal, *, pay_date: date,
) -> None:
    async with AsyncSessionLocal() as session:
        pay = await pay_svc.create_draft(
            session,
            company_id=company,
            contact_id=contact,
            bank_account_id=bank,
            payment_date=pay_date,
            amount=amount,
            direction=PaymentDirection.INCOMING,
        )
    async with AsyncSessionLocal() as session:
        await pay_svc.post_payment(session, pay.id, posted_by="test")
    async with AsyncSessionLocal() as session:
        await pay_svc.allocate(
            session, pay.id,
            invoice_allocations=[(invoice_id, amount)],
        )


async def _pay_bill(
    company: uuid.UUID, contact: uuid.UUID, bank: uuid.UUID,
    bill_id: uuid.UUID, amount: Decimal, *, pay_date: date,
) -> None:
    async with AsyncSessionLocal() as session:
        pay = await pay_svc.create_draft(
            session,
            company_id=company,
            contact_id=contact,
            bank_account_id=bank,
            payment_date=pay_date,
            amount=amount,
            direction=PaymentDirection.OUTGOING,
        )
    async with AsyncSessionLocal() as session:
        await pay_svc.post_payment(session, pay.id, posted_by="test")
    async with AsyncSessionLocal() as session:
        await pay_svc.allocate(
            session, pay.id,
            bill_allocations=[(bill_id, amount)],
        )


def _outstanding(report: svc.AgedReport, invoice_id: uuid.UUID) -> Decimal:
    for group in report.groups:
        for row in group.invoices:
            if row.invoice_id == invoice_id:
                return row.balance_due
    return Decimal("0")


# --------------------------------------------------------------------------- #
# AR — credit note dated after cutoff                                          #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_future_credit_note_does_not_clear_asof() -> None:
    company, contact, income, expense, bank, gst = await _make_company()
    inv = await _post_invoice(
        company, contact, income, gst, Decimal("100.00"),
        issue=date(2026, 6, 1),
    )  # total 110.00

    await _post_cn(
        company, contact, expense, gst, Decimal("100.00"),
        issue=date(2026, 6, 30), original_invoice_id=inv,
    )  # CN total 110.00, dated AFTER the cutoff below

    # As-of 2026-06-25 (before the CN) → still fully outstanding.
    async with AsyncSessionLocal() as session:
        report = await svc.aged_ar(session, company, as_at=date(2026, 6, 25))
    assert _outstanding(report, inv) == Decimal("110.00")

    # As-of 2026-06-30 (on/after the CN) → cleared.
    async with AsyncSessionLocal() as session:
        report = await svc.aged_ar(session, company, as_at=date(2026, 6, 30))
    assert _outstanding(report, inv) == Decimal("0")


# --------------------------------------------------------------------------- #
# AR — payment dated after cutoff                                             #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_future_payment_does_not_clear_asof() -> None:
    company, contact, income, expense, bank, gst = await _make_company()
    inv = await _post_invoice(
        company, contact, income, gst, Decimal("100.00"),
        issue=date(2026, 6, 1),
    )
    await _pay_invoice(
        company, contact, bank, inv, Decimal("110.00"),
        pay_date=date(2026, 6, 30),
    )

    async with AsyncSessionLocal() as session:
        report = await svc.aged_ar(session, company, as_at=date(2026, 6, 25))
    assert _outstanding(report, inv) == Decimal("110.00")

    async with AsyncSessionLocal() as session:
        report = await svc.aged_ar(session, company, as_at=date(2026, 6, 30))
    assert _outstanding(report, inv) == Decimal("0")


# --------------------------------------------------------------------------- #
# AR — settlements on/before cutoff still reduce (no regression)              #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_past_credit_note_and_payment_reduce_asof() -> None:
    company, contact, income, expense, bank, gst = await _make_company()

    # CN dated before cutoff settles fully.
    inv1 = await _post_invoice(
        company, contact, income, gst, Decimal("100.00"),
        issue=date(2026, 6, 1),
    )
    await _post_cn(
        company, contact, expense, gst, Decimal("100.00"),
        issue=date(2026, 6, 10), original_invoice_id=inv1,
    )

    # Payment dated before cutoff settles partially.
    inv2 = await _post_invoice(
        company, contact, income, gst, Decimal("200.00"),
        issue=date(2026, 6, 1),
    )  # total 220.00
    await _pay_invoice(
        company, contact, bank, inv2, Decimal("100.00"),
        pay_date=date(2026, 6, 12),
    )

    async with AsyncSessionLocal() as session:
        report = await svc.aged_ar(session, company, as_at=date(2026, 6, 25))
    assert _outstanding(report, inv1) == Decimal("0")  # CN cleared it
    assert _outstanding(report, inv2) == Decimal("120.00")  # 220 - 100


# --------------------------------------------------------------------------- #
# AP — payment dated after cutoff                                            #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_ap_future_payment_does_not_clear_asof() -> None:
    company, contact, income, expense, bank, gst = await _make_company()
    bill = await _post_bill(
        company, contact, expense, gst, Decimal("100.00"),
        issue=date(2026, 6, 1),
    )  # total 110.00
    await _pay_bill(
        company, contact, bank, bill, Decimal("110.00"),
        pay_date=date(2026, 6, 30),
    )

    async with AsyncSessionLocal() as session:
        report = await svc.aged_ap(session, company, as_at=date(2026, 6, 25))
    assert _outstanding(report, bill) == Decimal("110.00")

    async with AsyncSessionLocal() as session:
        report = await svc.aged_ap(session, company, as_at=date(2026, 6, 30))
    assert _outstanding(report, bill) == Decimal("0")
