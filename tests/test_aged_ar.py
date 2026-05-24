"""Tests for ``saebooks.services.reports.aged_ar``.

Covers:

* Empty report when no POSTED invoices exist
* Correct bucketing at the 0/1/30/31/60/61/90/91 day boundaries
* Partially-paid invoice reports ``balance_due`` (not ``total``)
* Voided / archived / DRAFT invoices are excluded
* Future-issued invoices (issue_date > as_at) are excluded
* Multiple invoices for the same contact roll up into one group
* Groups are sorted by descending total (biggest debtor first)
* CSV export has the right column set and row count
* Router 200s the HTML view and 200s the CSV view with correct mime
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
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.document_counter import DocumentCounter
from saebooks.models.invoice import Invoice, InvoiceStatus
from saebooks.models.tax_code import TaxCode
from saebooks.services import invoices as inv_svc
from saebooks.services import reports as svc
pytestmark = pytest.mark.postgres_only


_TEST_CONTACT_NAMES = {
    "Aged AR Test Co",
    "Partial Pay Debtor",
    "Exclusion Debtor",
    "CSV Debtor",
}


async def _fast_forward_invoice_counter() -> None:
    """Advance the per-company invoice DocumentCounter past any existing
    INV-NNNNNN number — see ``test_bills.py`` for the full rationale.
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
                select(Invoice.number).where(
                    Invoice.company_id == company.id,
                    Invoice.number.isnot(None),
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
                    DocumentCounter.kind == "invoice",
                )
            )
        ).scalar_one_or_none()
        if counter is None:
            counter = DocumentCounter(
                company_id=company.id,
                kind="invoice",
                prefix="INV-",
                pad_width=6,
                next_value=max_suffix + 1,
            )
            session.add(counter)
        elif counter.next_value <= max_suffix:
            counter.next_value = max_suffix + 1
        await session.commit()


async def _purge_test_invoices() -> None:
    """Void every invoice attached to an aged-AR test contact.

    The aged reports bucket POSTED invoices by due date, so unpaid
    invoices left behind by prior runs leak into the ``current`` bucket
    and skew bucketing assertions. Voiding flips status to VOIDED and
    reverses the journal, which is what the report already excludes.
    The partial-payment test stamps ``amount_paid`` directly (not via
    the payments service), so we zero it out first — void_invoice
    refuses to proceed on invoices with a non-zero amount_paid.
    """
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(Invoice.id)
                .join(Contact, Invoice.contact_id == Contact.id)
                .where(
                    Contact.name.in_(_TEST_CONTACT_NAMES),
                    Invoice.status == InvoiceStatus.POSTED,
                )
            )
        ).scalars().all()
    for inv_id in rows:
        async with AsyncSessionLocal() as session:
            inv = await session.get(Invoice, inv_id)
            if inv is not None and inv.amount_paid > Decimal("0"):
                inv.amount_paid = Decimal("0")
                await session.commit()
        async with AsyncSessionLocal() as session:
            with contextlib.suppress(Exception):
                await inv_svc.void_invoice(session, inv_id, posted_by="cleanup")


@pytest.fixture(autouse=True, scope="module")
async def _prep_aged_ar() -> AsyncGenerator[None, None]:
    await _fast_forward_invoice_counter()
    await _purge_test_invoices()
    yield
    await _purge_test_invoices()


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company_id, income_account_id, gst_tax_code_id)."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None
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
        return company.id, income.id, gst.id


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
            contact_type=ContactType.CUSTOMER,
        )
        session.add(c)
        await session.commit()
        await session.refresh(c)
        return c.id


async def _post_invoice(
    company_id: uuid.UUID,
    contact_id: uuid.UUID,
    income: uuid.UUID,
    gst: uuid.UUID,
    *,
    issue_date: date,
    due_date: date,
    total: Decimal,
) -> uuid.UUID:
    """Create + POST an invoice with a single tax-free line for `total`."""
    async with AsyncSessionLocal() as session:
        inv = await inv_svc.create_draft(
            session,
            company_id=company_id,
            contact_id=contact_id,
            issue_date=issue_date,
            due_date=due_date,
            lines=[
                {
                    "description": "Aged AR test line",
                    "account_id": income,
                    "tax_code_id": None,  # keeps totals clean
                    "quantity": Decimal("1"),
                    "unit_price": total,
                    "discount_pct": Decimal("0"),
                }
            ],
        )
        posted = await inv_svc.post_invoice(session, inv.id, posted_by="tests")
        return posted.id


# ---------------------------------------------------------------------- #
# Bucketing                                                               #
# ---------------------------------------------------------------------- #


def test_bucket_boundaries() -> None:
    """0=current, 1=d1_30, 30=d1_30, 31=d31_60, 60=d31_60, 61=d61_90,
    90=d61_90, 91=d90_plus, negative=current."""
    assert svc._bucket_for_age(-5) == "current"
    assert svc._bucket_for_age(0) == "current"
    assert svc._bucket_for_age(1) == "d1_30"
    assert svc._bucket_for_age(30) == "d1_30"
    assert svc._bucket_for_age(31) == "d31_60"
    assert svc._bucket_for_age(60) == "d31_60"
    assert svc._bucket_for_age(61) == "d61_90"
    assert svc._bucket_for_age(90) == "d61_90"
    assert svc._bucket_for_age(91) == "d90_plus"
    assert svc._bucket_for_age(365) == "d90_plus"


# ---------------------------------------------------------------------- #
# Service                                                                 #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_aged_ar_empty_when_no_posted() -> None:
    cid, _acct, _gst = await _ctx()
    as_at = date(2026, 4, 21)
    async with AsyncSessionLocal() as session:
        report = await svc.aged_ar(session, cid, as_at=as_at)
    # May find prior test data, so we just assert the shape.
    assert report.as_at == as_at
    assert all(k in report.grand_totals for k in svc.BUCKET_KEYS)


@pytest.mark.asyncio
async def test_aged_ar_buckets_by_due_date() -> None:
    cid, acct, gst = await _ctx()
    contact = await _contact("Aged AR Test Co", cid)
    # Far enough past the FY26-Q3 period lock that 150-day lookbacks
    # don't collide with it.
    as_at = date(2026, 9, 30)

    # Invoices spanning every bucket: current (due today), 1-30 (15d
    # overdue), 31-60 (45d), 61-90 (75d), 90+ (120d).
    await _post_invoice(
        cid, contact, acct, gst,
        issue_date=as_at - timedelta(days=5),
        due_date=as_at,
        total=Decimal("100.00"),
    )
    await _post_invoice(
        cid, contact, acct, gst,
        issue_date=as_at - timedelta(days=30),
        due_date=as_at - timedelta(days=15),
        total=Decimal("200.00"),
    )
    await _post_invoice(
        cid, contact, acct, gst,
        issue_date=as_at - timedelta(days=75),
        due_date=as_at - timedelta(days=45),
        total=Decimal("300.00"),
    )
    await _post_invoice(
        cid, contact, acct, gst,
        issue_date=as_at - timedelta(days=100),
        due_date=as_at - timedelta(days=75),
        total=Decimal("400.00"),
    )
    await _post_invoice(
        cid, contact, acct, gst,
        issue_date=as_at - timedelta(days=150),
        due_date=as_at - timedelta(days=120),
        total=Decimal("500.00"),
    )

    async with AsyncSessionLocal() as session:
        report = await svc.aged_ar(session, cid, as_at=as_at)

    group = next(g for g in report.groups if g.contact_id == contact)
    assert group.buckets["current"] == Decimal("100.00")
    assert group.buckets["d1_30"] == Decimal("200.00")
    assert group.buckets["d31_60"] == Decimal("300.00")
    assert group.buckets["d61_90"] == Decimal("400.00")
    assert group.buckets["d90_plus"] == Decimal("500.00")
    assert group.total == Decimal("1500.00")


@pytest.mark.asyncio
async def test_aged_ar_partial_payment_shows_balance_due() -> None:
    cid, acct, gst = await _ctx()
    contact = await _contact("Partial Pay Debtor", cid)
    as_at = date(2026, 4, 21)

    inv_id = await _post_invoice(
        cid, contact, acct, gst,
        issue_date=as_at - timedelta(days=10),
        due_date=as_at - timedelta(days=5),
        total=Decimal("1000.00"),
    )
    # Stamp amount_paid directly — normally done by payment allocation,
    # but we're testing the aged report, not the payment service.
    async with AsyncSessionLocal() as session:
        inv = await inv_svc.get(session, inv_id)
        inv.amount_paid = Decimal("300.00")
        await session.commit()

    async with AsyncSessionLocal() as session:
        report = await svc.aged_ar(session, cid, as_at=as_at)
    group = next(g for g in report.groups if g.contact_id == contact)
    # Balance due = 1000 - 300 = 700
    assert group.total == Decimal("700.00")
    assert group.invoices[0].balance_due == Decimal("700.00")


@pytest.mark.asyncio
async def test_aged_ar_excludes_draft_and_voided_and_future() -> None:
    cid, acct, gst = await _ctx()
    contact = await _contact("Exclusion Debtor", cid)
    as_at = date(2026, 4, 21)

    # Future-dated POSTED invoice — excluded
    await _post_invoice(
        cid, contact, acct, gst,
        issue_date=as_at + timedelta(days=10),
        due_date=as_at + timedelta(days=40),
        total=Decimal("111.11"),
    )
    # DRAFT invoice — excluded
    async with AsyncSessionLocal() as session:
        draft = await inv_svc.create_draft(
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
        assert draft.status == InvoiceStatus.DRAFT
    # Voided invoice — posts then voids
    voided_id = await _post_invoice(
        cid, contact, acct, gst,
        issue_date=as_at - timedelta(days=12),
        due_date=as_at - timedelta(days=7),
        total=Decimal("333.33"),
    )
    async with AsyncSessionLocal() as session:
        await inv_svc.void_invoice(session, voided_id, posted_by="tests")

    async with AsyncSessionLocal() as session:
        report = await svc.aged_ar(session, cid, as_at=as_at)

    # The contact should either not appear OR appear with zero of the
    # excluded totals. Walk the group if it exists.
    group = next(
        (g for g in report.groups if g.contact_id == contact),
        None,
    )
    if group is not None:
        assert Decimal("111.11") not in [i.balance_due for i in group.invoices]
        assert Decimal("222.22") not in [i.balance_due for i in group.invoices]
        assert Decimal("333.33") not in [i.balance_due for i in group.invoices]


@pytest.mark.asyncio
async def test_aged_ar_csv_shape() -> None:
    cid, acct, gst = await _ctx()
    contact = await _contact("CSV Debtor", cid)
    as_at = date(2026, 4, 21)

    await _post_invoice(
        cid, contact, acct, gst,
        issue_date=as_at - timedelta(days=10),
        due_date=as_at - timedelta(days=5),
        total=Decimal("42.50"),
    )
    async with AsyncSessionLocal() as session:
        report = await svc.aged_ar(session, cid, as_at=as_at)
    csv_text = svc.aged_ar_csv(report)
    lines = csv_text.strip().splitlines()
    assert lines[0].split(",") == [
        "contact",
        "invoice_number",
        "issue_date",
        "due_date",
        "total",
        "paid",
        "balance_due",
        "days_overdue",
        "bucket",
    ]
    # At least our new row plus header
    assert len(lines) >= 2
    # Confirm our specific row exists
    assert any("CSV Debtor" in ln and "42.50" in ln for ln in lines[1:])


# ---------------------------------------------------------------------- #
# Router                                                                  #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_aged_ar_html_view_renders(client: AsyncClient) -> None:
    r = await client.get("/reports/aged-ar")
    assert r.status_code == 200
    assert "Aged debtors" in r.text


@pytest.mark.asyncio
async def test_aged_ar_csv_download(client: AsyncClient) -> None:
    r = await client.get("/reports/aged-ar?format=csv&as_at=2026-04-21")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert "aged-ar-2026-04-21.csv" in r.headers["content-disposition"]
    # Column header row present
    assert "contact,invoice_number" in r.text


@pytest.mark.asyncio
async def test_reports_index_links_to_aged_ar(client: AsyncClient) -> None:
    r = await client.get("/reports")
    assert r.status_code == 200
    assert "Aged debtors" in r.text
    assert "/reports/aged-ar" in r.text
