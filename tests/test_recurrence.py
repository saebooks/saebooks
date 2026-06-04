"""Tests for ``saebooks.services.recurrence``.

Covers:

* ``advance()`` date arithmetic across all five cadences
* Month-end anchor-day safety: 31-Jan → 28-Feb → 31-Mar, no drift
* Leap-year handling: 31-Jan-2028 → 29-Feb-2028
* Quarterly + yearly month-end safety
* ``create()`` validates lines non-empty + due_days + end_date bounds
* ``materialise_one()`` forks a DRAFT invoice, advances next_run,
  stamps last_run, increments invoices_generated
* ``materialise_one()`` with ``auto_post=True`` mints an invoice number
  and flips status to POSTED
* ``due_today()`` filters to ACTIVE templates with next_run <= as_of
* PAUSED/ENDED templates are NOT returned by ``due_today()``
* Idempotence: a second ``run_due()`` on the same day yields nothing
* ``end_date`` stops the schedule once new next_run > end_date
* ``pause`` / ``resume`` / ``end`` lifecycle transitions
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.invoice import InvoiceStatus
from saebooks.models.recurring_invoice import (
    RecurrenceFrequency,
    RecurrenceStatus,
)
from saebooks.models.tax_code import TaxCode
from saebooks.services import recurrence as svc

pytestmark = pytest.mark.postgres_only


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company_id, contact_id, income_account_id, gst_tax_code_id)."""
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
        contact = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == company.id,
                    Contact.name == "Recurring Test Customer",
                )
            )
        ).scalars().first()
        if contact is None:
            contact = Contact(
                company_id=company.id,
                name="Recurring Test Customer",
                contact_type=ContactType.CUSTOMER,
                email="recurring@example.com",
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)
        return company.id, contact.id, income.id, gst.id


def _line(acct: uuid.UUID, gst: uuid.UUID, unit: Decimal) -> dict[str, object]:
    return {
        "description": "Monthly retainer",
        "account_id": acct,
        "tax_code_id": gst,
        "quantity": Decimal("1"),
        "unit_price": unit,
        "discount_pct": Decimal("0"),
    }


# ---------------------------------------------------------------------- #
# advance() pure logic                                                    #
# ---------------------------------------------------------------------- #


def test_advance_weekly() -> None:
    assert svc.advance(
        date(2026, 4, 21), RecurrenceFrequency.WEEKLY, None
    ) == date(2026, 4, 28)


def test_advance_fortnightly() -> None:
    assert svc.advance(
        date(2026, 4, 21), RecurrenceFrequency.FORTNIGHTLY, None
    ) == date(2026, 5, 5)


def test_advance_monthly_midmonth_stays_on_anchor() -> None:
    assert svc.advance(
        date(2026, 4, 15), RecurrenceFrequency.MONTHLY, 15
    ) == date(2026, 5, 15)


def test_advance_monthly_end_of_month_caps_at_shorter_month() -> None:
    # 31-Jan → 28-Feb, anchor remembered as 31
    assert svc.advance(
        date(2026, 1, 31), RecurrenceFrequency.MONTHLY, 31
    ) == date(2026, 2, 28)


def test_advance_monthly_end_of_month_climbs_back_to_31() -> None:
    # After the Feb cap, the next month restores anchor 31.
    assert svc.advance(
        date(2026, 2, 28), RecurrenceFrequency.MONTHLY, 31
    ) == date(2026, 3, 31)


def test_advance_monthly_leap_year_29th() -> None:
    assert svc.advance(
        date(2028, 1, 31), RecurrenceFrequency.MONTHLY, 31
    ) == date(2028, 2, 29)


def test_advance_quarterly_31st_caps() -> None:
    # 31-Jan + 3 months = 30-Apr (Apr has 30 days)
    assert svc.advance(
        date(2026, 1, 31), RecurrenceFrequency.QUARTERLY, 31
    ) == date(2026, 4, 30)


def test_advance_yearly_leap_day() -> None:
    # 29-Feb 2028 + 12 months → 28-Feb 2029 (non-leap), anchor 29.
    assert svc.advance(
        date(2028, 2, 29), RecurrenceFrequency.YEARLY, 29
    ) == date(2029, 2, 28)


# ---------------------------------------------------------------------- #
# create() validation                                                     #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_requires_lines() -> None:
    cid, contact, _acct, _gst = await _ctx()
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.RecurrenceError):
            await svc.create(
                session,
                company_id=cid,
                contact_id=contact,
                name="Empty",
                frequency=RecurrenceFrequency.MONTHLY,
                next_run=date(2026, 5, 1),
                lines=[],
            )


@pytest.mark.asyncio
async def test_create_rejects_end_date_before_next_run() -> None:
    cid, contact, acct, gst = await _ctx()
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.RecurrenceError):
            await svc.create(
                session,
                company_id=cid,
                contact_id=contact,
                name="Backwards",
                frequency=RecurrenceFrequency.MONTHLY,
                next_run=date(2026, 6, 1),
                end_date=date(2026, 5, 1),
                lines=[_line(acct, gst, Decimal("100.00"))],
            )


@pytest.mark.asyncio
async def test_create_seeds_anchor_day_from_next_run() -> None:
    cid, contact, acct, gst = await _ctx()
    async with AsyncSessionLocal() as session:
        tpl = await svc.create(
            session,
            company_id=cid,
            contact_id=contact,
            name="Anchor seed",
            frequency=RecurrenceFrequency.MONTHLY,
            next_run=date(2026, 1, 31),
            lines=[_line(acct, gst, Decimal("200.00"))],
        )
        assert tpl.anchor_day == 31


# ---------------------------------------------------------------------- #
# materialise_one()                                                       #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_materialise_one_creates_draft_invoice_and_advances() -> None:
    cid, contact, acct, gst = await _ctx()
    async with AsyncSessionLocal() as session:
        tpl = await svc.create(
            session,
            company_id=cid,
            contact_id=contact,
            name="Materialise test 1",
            frequency=RecurrenceFrequency.MONTHLY,
            next_run=date(2026, 4, 21),
            due_days=14,
            lines=[_line(acct, gst, Decimal("300.00"))],
        )
        inv = await svc.materialise_one(
            session, tpl, as_of=date(2026, 4, 21)
        )
        assert inv.status == InvoiceStatus.DRAFT
        assert inv.number is None  # DRAFT never burns a number
        assert inv.issue_date == date(2026, 4, 21)
        assert inv.due_date == date(2026, 5, 5)  # +14 days
        assert len(inv.lines) == 1
        assert inv.lines[0].unit_price == Decimal("300.0000")
        # Template advanced
        tpl_after = await svc.get(session, tpl.id)
        assert tpl_after.last_run == date(2026, 4, 21)
        assert tpl_after.invoices_generated == 1
        assert tpl_after.next_run == date(2026, 5, 21)


@pytest.mark.asyncio
async def test_materialise_one_with_auto_post_mints_number() -> None:
    cid, contact, acct, gst = await _ctx()
    async with AsyncSessionLocal() as session:
        tpl = await svc.create(
            session,
            company_id=cid,
            contact_id=contact,
            name="Auto-post test",
            frequency=RecurrenceFrequency.MONTHLY,
            next_run=date(2026, 4, 21),
            auto_post=True,
            lines=[_line(acct, gst, Decimal("100.00"))],
        )
        inv = await svc.materialise_one(
            session, tpl, as_of=date(2026, 4, 21)
        )
        assert inv.status == InvoiceStatus.POSTED
        assert inv.number is not None
        assert inv.number.startswith("INV-")
        assert inv.total == Decimal("110.00")  # 100 + 10% GST


@pytest.mark.asyncio
async def test_materialise_one_rejects_future_dated_template() -> None:
    cid, contact, acct, gst = await _ctx()
    async with AsyncSessionLocal() as session:
        tpl = await svc.create(
            session,
            company_id=cid,
            contact_id=contact,
            name="Future-dated",
            frequency=RecurrenceFrequency.MONTHLY,
            next_run=date(2027, 1, 1),
            lines=[_line(acct, gst, Decimal("50.00"))],
        )
        with pytest.raises(svc.RecurrenceError):
            await svc.materialise_one(
                session, tpl, as_of=date(2026, 4, 21)
            )


@pytest.mark.asyncio
async def test_materialise_one_end_date_closes_schedule() -> None:
    """When the new next_run overshoots end_date, flip to ENDED."""
    cid, contact, acct, gst = await _ctx()
    async with AsyncSessionLocal() as session:
        tpl = await svc.create(
            session,
            company_id=cid,
            contact_id=contact,
            name="End-date test",
            frequency=RecurrenceFrequency.MONTHLY,
            next_run=date(2026, 4, 21),
            end_date=date(2026, 5, 1),  # Before next next_run of May 21
            lines=[_line(acct, gst, Decimal("50.00"))],
        )
        await svc.materialise_one(session, tpl, as_of=date(2026, 4, 21))
        tpl_after = await svc.get(session, tpl.id)
        assert tpl_after.status == RecurrenceStatus.ENDED


# ---------------------------------------------------------------------- #
# due_today() + run_due() + idempotency                                   #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_due_today_filters_to_active_and_due() -> None:
    cid, contact, acct, gst = await _ctx()
    async with AsyncSessionLocal() as session:
        t_due = await svc.create(
            session,
            company_id=cid,
            contact_id=contact,
            name="Due test A",
            frequency=RecurrenceFrequency.WEEKLY,
            next_run=date(2026, 4, 20),
            lines=[_line(acct, gst, Decimal("10.00"))],
        )
        t_future = await svc.create(
            session,
            company_id=cid,
            contact_id=contact,
            name="Due test B (future)",
            frequency=RecurrenceFrequency.WEEKLY,
            next_run=date(2026, 5, 1),
            lines=[_line(acct, gst, Decimal("10.00"))],
        )
        t_paused = await svc.create(
            session,
            company_id=cid,
            contact_id=contact,
            name="Due test C (paused)",
            frequency=RecurrenceFrequency.WEEKLY,
            next_run=date(2026, 4, 20),
            lines=[_line(acct, gst, Decimal("10.00"))],
        )
        await svc.pause(session, t_paused.id)

        due = await svc.due_today(
            session, as_of=date(2026, 4, 21), company_id=cid
        )
        due_ids = {t.id for t in due}
        assert t_due.id in due_ids
        assert t_future.id not in due_ids
        assert t_paused.id not in due_ids


@pytest.mark.asyncio
async def test_run_due_is_idempotent_on_same_day() -> None:
    cid, contact, acct, gst = await _ctx()
    async with AsyncSessionLocal() as session:
        tpl = await svc.create(
            session,
            company_id=cid,
            contact_id=contact,
            name="Idempotent test",
            frequency=RecurrenceFrequency.WEEKLY,
            next_run=date(2026, 4, 21),
            lines=[_line(acct, gst, Decimal("25.00"))],
        )
        first = await svc.run_due(
            session, as_of=date(2026, 4, 21), company_id=cid
        )
        # Filter to invoices from this specific template to avoid
        # cross-test contamination.
        first_for_tpl = [
            inv for inv in first if inv.contact_id == contact
        ]
        assert len(first_for_tpl) >= 1
        second = await svc.run_due(
            session, as_of=date(2026, 4, 21), company_id=cid
        )
        second_for_tpl = [
            inv for inv in second if inv.contact_id == contact
        ]
        assert second_for_tpl == []
        tpl_after = await svc.get(session, tpl.id)
        assert tpl_after.invoices_generated == 1


# ---------------------------------------------------------------------- #
# Lifecycle                                                               #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pause_then_resume_cycles_status() -> None:
    cid, contact, acct, gst = await _ctx()
    async with AsyncSessionLocal() as session:
        tpl = await svc.create(
            session,
            company_id=cid,
            contact_id=contact,
            name="Lifecycle test",
            frequency=RecurrenceFrequency.MONTHLY,
            next_run=date(2026, 5, 1),
            lines=[_line(acct, gst, Decimal("50.00"))],
        )
        paused = await svc.pause(session, tpl.id)
        assert paused.status == RecurrenceStatus.PAUSED
        resumed = await svc.resume(session, tpl.id)
        assert resumed.status == RecurrenceStatus.ACTIVE
        ended = await svc.end(session, tpl.id)
        assert ended.status == RecurrenceStatus.ENDED
        # Can't resume an ENDED schedule
        with pytest.raises(svc.RecurrenceError):
            await svc.resume(session, tpl.id)
