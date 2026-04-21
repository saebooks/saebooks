"""Tests for ``saebooks.services.reports.cashflow_forecast``.

Covers:

* Empty horizon still returns a well-shaped report (weekly buckets filled).
* Open POSTED invoice adds to ``total_inflows`` on ``due_date``.
* Overdue invoices collapse onto ``as_of`` (land in week 0).
* ``horizon_days`` clamps the window — invoices due beyond it are excluded.
* Weekly roll-up math: ``running_balance[i] = opening + sum(net[0..i])``.
* Projected closing = opening + inflows - outflows.
* Router 200s the HTML view.
"""
from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncGenerator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.document_counter import DocumentCounter
from saebooks.models.invoice import Invoice
from saebooks.models.tax_code import TaxCode
from saebooks.services import invoices as inv_svc
from saebooks.services import reports as svc


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """(company_id, income_account_id, any_tax_code_id)."""
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
    *,
    issue_date: date,
    due_date: date,
    total: Decimal,
) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        inv = await inv_svc.create_draft(
            session,
            company_id=company_id,
            contact_id=contact_id,
            issue_date=issue_date,
            due_date=due_date,
            lines=[
                {
                    "description": "cashflow test",
                    "account_id": income,
                    "tax_code_id": None,
                    "quantity": Decimal("1"),
                    "unit_price": total,
                    "discount_pct": Decimal("0"),
                }
            ],
        )
        posted = await inv_svc.post_invoice(session, inv.id, posted_by="tests")
        return posted.id


# ---------------------------------------------------------------------- #
# Service                                                                 #
# ---------------------------------------------------------------------- #


async def test_cashflow_shape_is_valid_on_empty_company() -> None:
    """Even a quiet company gets the right weekly roll-up structure."""
    cid, _income, _gst = await _ctx()
    as_of = date(2099, 1, 1)
    async with AsyncSessionLocal() as session:
        report = await svc.cashflow_forecast(
            session, cid,
            as_of=as_of, horizon_days=28,
        )
    assert report.from_date == as_of
    assert report.to_date == as_of + timedelta(days=28)
    # 28 / 7 = 4 full weeks, + the start-of-day-28 boundary = 5 buckets
    assert len(report.weeks) == 5
    # Opening computed from real GL — might be non-zero from other tests
    # but projected_closing must always equal the identity.
    expected = report.opening_balance + report.total_inflows - report.total_outflows
    assert report.projected_closing == expected


async def test_cashflow_inflow_lands_in_week_containing_due_date() -> None:
    cid, income, _gst = await _ctx()
    contact = await _contact("Cashflow Customer", cid)
    as_of = date(2099, 2, 1)
    # Invoice due 10 days out → should land in week 1 (days 7-13)
    await _post_invoice(
        cid, contact, income,
        issue_date=as_of,
        due_date=as_of + timedelta(days=10),
        total=Decimal("500"),
    )
    async with AsyncSessionLocal() as session:
        report = await svc.cashflow_forecast(
            session, cid,
            as_of=as_of, horizon_days=60,
        )
    # Filter by contact name — amount alone isn't unique on a
    # persistent dev DB where other test suites may have left 500-dollar
    # invoices lying around.
    our_item = next(
        (
            i for i in report.items
            if i.amount == Decimal("500")
            and "Cashflow Customer" in i.description
        ),
        None,
    )
    assert our_item is not None
    assert our_item.expected_date == as_of + timedelta(days=10)
    # Week index (10 // 7 = 1)
    assert report.weeks[1].inflows >= Decimal("500")


async def test_cashflow_overdue_collapses_to_as_of() -> None:
    cid, income, _gst = await _ctx()
    contact = await _contact("Overdue Customer", cid)
    as_of = date(2099, 3, 1)
    # Overdue: issued 60 days ago, due 30 days ago
    await _post_invoice(
        cid, contact, income,
        issue_date=as_of - timedelta(days=60),
        due_date=as_of - timedelta(days=30),
        total=Decimal("777"),
    )
    async with AsyncSessionLocal() as session:
        report = await svc.cashflow_forecast(
            session, cid,
            as_of=as_of, horizon_days=30,
        )
    our_item = next(
        (i for i in report.items if i.amount == Decimal("777")),
        None,
    )
    assert our_item is not None
    # Collapsed to as_of, not left at the original due_date
    assert our_item.expected_date == as_of


async def test_cashflow_horizon_excludes_far_future_invoices() -> None:
    cid, income, _gst = await _ctx()
    contact = await _contact("Far Future Customer", cid)
    as_of = date(2099, 4, 1)
    # Due 200 days out — outside a 30-day horizon
    await _post_invoice(
        cid, contact, income,
        issue_date=as_of,
        due_date=as_of + timedelta(days=200),
        total=Decimal("999.99"),
    )
    async with AsyncSessionLocal() as session:
        report = await svc.cashflow_forecast(
            session, cid,
            as_of=as_of, horizon_days=30,
        )
    amounts = {i.amount for i in report.items}
    assert Decimal("999.99") not in amounts


async def test_cashflow_running_balance_math() -> None:
    cid, _income, _gst = await _ctx()
    as_of = date(2099, 5, 1)
    async with AsyncSessionLocal() as session:
        report = await svc.cashflow_forecast(
            session, cid,
            as_of=as_of, horizon_days=21,
        )
    running = report.opening_balance
    for wk in report.weeks:
        running += wk.net
        assert wk.running_balance == running


async def test_cashflow_router_renders(client) -> None:  # type: ignore[no-untyped-def]
    r = await client.get("/reports/cashflow-forecast?horizon=30")
    assert r.status_code == 200
    assert "Cashflow forecast" in r.text


async def test_cashflow_index_card(client) -> None:  # type: ignore[no-untyped-def]
    r = await client.get("/reports")
    assert r.status_code == 200
    assert "/reports/cashflow-forecast" in r.text


# ---------------------------------------------------------------------- #
# Cleanup                                                                 #
# ---------------------------------------------------------------------- #


async def _fast_forward_invoice_counter() -> None:
    """Advance the per-company invoice DocumentCounter past any existing
    INV-NNNNNN number already in the DB.

    The dev DB is persistent — prior test runs + real UI clicks can
    leave the counter behind the highest extant invoice number, which
    causes ``IntegrityError: uq_invoices_company_number`` when a new
    test tries to post an invoice. We scan for the max integer suffix
    on ``invoices.number`` for the seeded company and nudge the counter
    to ``max + 1`` so the next ``next_number(... "invoice")`` mints a
    free number.
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
            # Numbers look like "INV-000042"; take the trailing int.
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


@pytest.fixture(autouse=True, scope="module")
async def _cleanup_test_invoices() -> AsyncGenerator[None, None]:
    """Void every sentinel-year cashflow test invoice so the persistent
    dev DB doesn't accumulate unpaid invoices that skew future runs.
    """
    await _fast_forward_invoice_counter()
    yield
    async with AsyncSessionLocal() as session:
        # Find every invoice issued in 2099 attached to our test
        # contacts — they all have names matching one of the four fixtures.
        test_contact_names = {
            "Cashflow Customer",
            "Overdue Customer",
            "Far Future Customer",
        }
        rows = (
            await session.execute(
                select(Invoice, Contact)
                .join(Contact, Invoice.contact_id == Contact.id)
                .where(
                    Contact.name.in_(test_contact_names),
                    Invoice.issue_date >= date(2099, 1, 1),
                    Invoice.issue_date < date(2100, 1, 1),
                )
            )
        ).all()
        for inv, _contact in rows:
            # Best-effort cleanup; skip if already voided or blocked.
            with contextlib.suppress(Exception):
                await inv_svc.void_invoice(session, inv.id, posted_by="cleanup")
