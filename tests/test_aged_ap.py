"""Tests for ``saebooks.services.reports.aged_ap``.

Mirror of ``test_aged_ar.py`` but walking POSTED, non-archived bills.
"""
from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncGenerator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.bill import Bill, BillStatus
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.document_counter import DocumentCounter
from saebooks.models.tax_code import TaxCode
from saebooks.services import bills as bill_svc
from saebooks.services import reports as svc

pytestmark = pytest.mark.postgres_only


_TEST_CONTACT_NAMES = {
    "Aged AP Test Co",
    "Partial Pay Creditor",
    "AP Exclusion Creditor",
    "CSV Creditor",
}


async def _fast_forward_bill_counter() -> None:
    """Advance the per-company bill DocumentCounter past any existing
    BILL-NNNNNN number — see ``test_bills.py`` for the full rationale.
    """
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
                select(Bill.number).where(
                    Bill.company_id == company.id,
                    Bill.number.isnot(None),
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
                    DocumentCounter.kind == "bill",
                )
            )
        ).scalar_one_or_none()
        if counter is None:
            counter = DocumentCounter(
                company_id=company.id,
                kind="bill",
                prefix="BILL-",
                pad_width=6,
                next_value=max_suffix + 1,
            )
            session.add(counter)
        elif counter.next_value <= max_suffix:
            counter.next_value = max_suffix + 1
        await session.commit()


async def _purge_test_bills() -> None:
    """Void every bill attached to an aged-AP test contact.

    The aged reports bucket POSTED bills by due date, so unpaid bills
    left behind by prior runs leak into the ``current`` bucket and
    skew bucketing assertions. Voiding flips status to VOIDED and
    reverses the journal, which is what the report already excludes.
    The partial-payment test stamps ``amount_paid`` directly (not via
    the payments service), so we zero it out first — void_bill
    refuses to proceed on bills with a non-zero amount_paid.
    """
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(Bill.id)
                .join(Contact, Bill.contact_id == Contact.id)
                .where(
                    Contact.name.in_(_TEST_CONTACT_NAMES),
                    Bill.status == BillStatus.POSTED,
                )
            )
        ).scalars().all()
    for bill_id in rows:
        async with AsyncSessionLocal() as session:
            bill = await session.get(Bill, bill_id)
            if bill is not None and bill.amount_paid > Decimal("0"):
                bill.amount_paid = Decimal("0")
                await session.commit()
        async with AsyncSessionLocal() as session:
            with contextlib.suppress(Exception):
                await bill_svc.void_bill(session, bill_id, posted_by="cleanup")


@pytest.fixture(autouse=True, scope="module")
async def _prep_aged_ap() -> AsyncGenerator[None, None]:
    await _fast_forward_bill_counter()
    await _purge_test_bills()
    yield
    await _purge_test_bills()


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company_id, expense_account_id, gst_tax_code_id)."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None
        expense = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.code == "6-1000",
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
        return company.id, expense.id, gst.id


async def _contact(name: str, company_id: uuid.UUID) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == company_id,
                    Contact.name == name,
                )
            )
        ).scalars().first()
        if existing is not None:
            return existing.id
        c = Contact(
            company_id=company_id,
            name=name,
            contact_type=ContactType.SUPPLIER,
        )
        session.add(c)
        await session.commit()
        await session.refresh(c)
        return c.id


async def _post_bill(
    company_id: uuid.UUID,
    contact_id: uuid.UUID,
    expense: uuid.UUID,
    gst: uuid.UUID,
    *,
    issue_date: date,
    due_date: date,
    total: Decimal,
) -> uuid.UUID:
    """Create + POST a bill with a single tax-free line for ``total``."""
    async with AsyncSessionLocal() as session:
        bill = await bill_svc.create_draft(
            session,
            company_id=company_id,
            contact_id=contact_id,
            issue_date=issue_date,
            due_date=due_date,
            lines=[
                {
                    "description": "Aged AP test line",
                    "account_id": expense,
                    "tax_code_id": None,  # keeps totals clean
                    "quantity": Decimal("1"),
                    "unit_price": total,
                    "discount_pct": Decimal("0"),
                }
            ],
        )
        posted = await bill_svc.post_bill(session, bill.id, posted_by="tests")
        return posted.id


# ---------------------------------------------------------------------- #
# Service                                                                 #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_aged_ap_empty_shape() -> None:
    cid, _acct, _gst = await _ctx()
    as_at = date(2026, 9, 30)
    async with AsyncSessionLocal() as session:
        report = await svc.aged_ap(session, cid, as_at=as_at)
    assert report.as_at == as_at
    assert all(k in report.grand_totals for k in svc.BUCKET_KEYS)


@pytest.mark.asyncio
async def test_aged_ap_buckets_by_due_date() -> None:
    cid, acct, gst = await _ctx()
    contact = await _contact("Aged AP Test Co", cid)
    # Far enough past the FY26-Q3 period lock that 150-day lookbacks
    # don't collide with it.
    as_at = date(2026, 9, 30)

    await _post_bill(
        cid, contact, acct, gst,
        issue_date=as_at - timedelta(days=5),
        due_date=as_at,
        total=Decimal("100.00"),
    )
    await _post_bill(
        cid, contact, acct, gst,
        issue_date=as_at - timedelta(days=30),
        due_date=as_at - timedelta(days=15),
        total=Decimal("200.00"),
    )
    await _post_bill(
        cid, contact, acct, gst,
        issue_date=as_at - timedelta(days=75),
        due_date=as_at - timedelta(days=45),
        total=Decimal("300.00"),
    )
    await _post_bill(
        cid, contact, acct, gst,
        issue_date=as_at - timedelta(days=100),
        due_date=as_at - timedelta(days=75),
        total=Decimal("400.00"),
    )
    await _post_bill(
        cid, contact, acct, gst,
        issue_date=as_at - timedelta(days=150),
        due_date=as_at - timedelta(days=120),
        total=Decimal("500.00"),
    )

    async with AsyncSessionLocal() as session:
        report = await svc.aged_ap(session, cid, as_at=as_at)

    group = next(g for g in report.groups if g.contact_id == contact)
    assert group.buckets["current"] == Decimal("100.00")
    assert group.buckets["d1_30"] == Decimal("200.00")
    assert group.buckets["d31_60"] == Decimal("300.00")
    assert group.buckets["d61_90"] == Decimal("400.00")
    assert group.buckets["d90_plus"] == Decimal("500.00")
    assert group.total == Decimal("1500.00")


@pytest.mark.asyncio
async def test_aged_ap_partial_payment_shows_balance_due() -> None:
    cid, acct, gst = await _ctx()
    contact = await _contact("Partial Pay Creditor", cid)
    as_at = date(2026, 4, 21)

    bill_id = await _post_bill(
        cid, contact, acct, gst,
        issue_date=as_at - timedelta(days=10),
        due_date=as_at - timedelta(days=5),
        total=Decimal("1000.00"),
    )
    # Stamp amount_paid directly — testing the report math, not allocation.
    async with AsyncSessionLocal() as session:
        bill = await bill_svc.get(session, bill_id)
        bill.amount_paid = Decimal("250.00")
        await session.commit()

    async with AsyncSessionLocal() as session:
        report = await svc.aged_ap(session, cid, as_at=as_at)
    group = next(g for g in report.groups if g.contact_id == contact)
    # Balance due = 1000 - 250 = 750
    assert group.total == Decimal("750.00")
    assert group.invoices[0].balance_due == Decimal("750.00")


@pytest.mark.asyncio
async def test_aged_ap_excludes_draft_voided_and_future() -> None:
    cid, acct, gst = await _ctx()
    contact = await _contact("AP Exclusion Creditor", cid)
    as_at = date(2026, 4, 21)

    # Future-dated POSTED bill — excluded
    await _post_bill(
        cid, contact, acct, gst,
        issue_date=as_at + timedelta(days=10),
        due_date=as_at + timedelta(days=40),
        total=Decimal("111.11"),
    )
    # DRAFT bill — excluded
    async with AsyncSessionLocal() as session:
        draft = await bill_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=as_at - timedelta(days=10),
            due_date=as_at - timedelta(days=5),
            lines=[
                {
                    "description": "draft",
                    "account_id": acct,
                    "tax_code_id": None,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("222.22"),
                    "discount_pct": Decimal("0"),
                }
            ],
        )
        assert draft.status == BillStatus.DRAFT
    # Voided bill — posts then voids
    voided_id = await _post_bill(
        cid, contact, acct, gst,
        issue_date=as_at - timedelta(days=12),
        due_date=as_at - timedelta(days=7),
        total=Decimal("333.33"),
    )
    async with AsyncSessionLocal() as session:
        await bill_svc.void_bill(session, voided_id, posted_by="tests")

    async with AsyncSessionLocal() as session:
        report = await svc.aged_ap(session, cid, as_at=as_at)

    group = next(
        (g for g in report.groups if g.contact_id == contact),
        None,
    )
    if group is not None:
        assert Decimal("111.11") not in [b.balance_due for b in group.invoices]
        assert Decimal("222.22") not in [b.balance_due for b in group.invoices]
        assert Decimal("333.33") not in [b.balance_due for b in group.invoices]


@pytest.mark.asyncio
async def test_aged_ap_csv_shape() -> None:
    cid, acct, gst = await _ctx()
    contact = await _contact("CSV Creditor", cid)
    as_at = date(2026, 4, 21)

    await _post_bill(
        cid, contact, acct, gst,
        issue_date=as_at - timedelta(days=10),
        due_date=as_at - timedelta(days=5),
        total=Decimal("42.50"),
    )
    async with AsyncSessionLocal() as session:
        report = await svc.aged_ap(session, cid, as_at=as_at)
    csv_text = svc.aged_ap_csv(report)
    lines = csv_text.strip().splitlines()
    assert lines[0].split(",") == [
        "contact",
        "bill_number",
        "issue_date",
        "due_date",
        "total",
        "paid",
        "balance_due",
        "days_overdue",
        "bucket",
    ]
    assert len(lines) >= 2
    assert any("CSV Creditor" in ln and "42.50" in ln for ln in lines[1:])


# ---------------------------------------------------------------------- #
# Router                                                                  #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_aged_ap_html_view_renders(client: AsyncClient) -> None:
    r = await client.get("/reports/aged-ap")
    assert r.status_code == 200
    assert "Aged creditors" in r.text


@pytest.mark.asyncio
async def test_aged_ap_csv_download(client: AsyncClient) -> None:
    r = await client.get("/reports/aged-ap?format=csv&as_at=2026-04-21")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert "aged-ap-2026-04-21.csv" in r.headers["content-disposition"]
    assert "contact,bill_number" in r.text


@pytest.mark.asyncio
async def test_reports_index_links_to_aged_ap(client: AsyncClient) -> None:
    r = await client.get("/reports")
    assert r.status_code == 200
    assert "Aged creditors" in r.text
    assert "/reports/aged-ap" in r.text
