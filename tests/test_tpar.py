"""Tests for TPAR report — service and router (gap CIVL-5)."""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.contact import Contact, ContactType
from saebooks.models.tax_code import TaxCode
from saebooks.services import bills as bill_svc
from saebooks.services import tpar as svc
from saebooks.services.companies import ensure_seed_company

pytestmark = pytest.mark.postgres_only


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with AsyncSessionLocal() as session:
        company = await ensure_seed_company(session)
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


async def _make_contact(
    name: str, company_id: uuid.UUID, *, is_tpar: bool
) -> uuid.UUID:
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
            existing.is_tpar_supplier = is_tpar
            await session.commit()
            return existing.id
        c = Contact(
            company_id=company_id,
            name=name,
            contact_type=ContactType.SUPPLIER,
            abn="51824753556",
            is_tpar_supplier=is_tpar,
        )
        session.add(c)
        await session.commit()
        await session.refresh(c)
        return c.id


async def _post_bill(
    company_id: uuid.UUID,
    contact_id: uuid.UUID,
    expense: uuid.UUID,
    gst_id: uuid.UUID,
    *,
    issue_date: date,
    total: Decimal,
) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        bill = await bill_svc.create_draft(
            session,
            company_id=company_id,
            contact_id=contact_id,
            issue_date=issue_date,
            due_date=issue_date,
            lines=[
                {
                    "description": "TPAR test",
                    "account_id": expense,
                    "tax_code_id": None,
                    "quantity": Decimal("1"),
                    "unit_price": total,
                    "discount_pct": Decimal("0"),
                }
            ],
        )
        posted = await bill_svc.post_bill(session, bill.id, posted_by="tests")
        return posted.id


# ------------------------------------------------------------------ #
# Service tests                                                        #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_tpar_empty_when_no_tpar_contacts() -> None:
    cid, _acct, _gst = await _ctx()
    async with AsyncSessionLocal() as session:
        report = await svc.tpar_report(
            session, cid,
            from_date=date(2040, 7, 1),
            to_date=date(2041, 6, 30),
        )
    assert report.payees == []
    assert report.grand_total_incl_gst == Decimal("0")


@pytest.mark.asyncio
async def test_tpar_includes_flagged_excludes_unflagged() -> None:
    cid, acct, gst = await _ctx()
    tpar_id = await _make_contact("TPAR Subbies Pty Ltd", cid, is_tpar=True)
    non_tpar_id = await _make_contact("Regular Supplier Co", cid, is_tpar=False)

    period_start = date(2027, 7, 1)
    period_end = date(2028, 6, 30)

    await _post_bill(cid, tpar_id, acct, gst, issue_date=date(2027, 9, 1), total=Decimal("25000.00"))
    await _post_bill(cid, non_tpar_id, acct, gst, issue_date=date(2027, 9, 1), total=Decimal("9999.00"))

    async with AsyncSessionLocal() as session:
        report = await svc.tpar_report(session, cid, from_date=period_start, to_date=period_end)

    payee_ids = {p.contact_id for p in report.payees}
    assert tpar_id in payee_ids
    assert non_tpar_id not in payee_ids


@pytest.mark.asyncio
async def test_tpar_aggregates_multiple_bills() -> None:
    cid, acct, gst = await _ctx()
    contact_id = await _make_contact("Multi Bill Subbie", cid, is_tpar=True)

    period_start = date(2028, 7, 1)
    period_end = date(2029, 6, 30)

    await _post_bill(cid, contact_id, acct, gst, issue_date=date(2028, 8, 15), total=Decimal("10000.00"))
    await _post_bill(cid, contact_id, acct, gst, issue_date=date(2028, 11, 20), total=Decimal("15000.00"))

    async with AsyncSessionLocal() as session:
        report = await svc.tpar_report(session, cid, from_date=period_start, to_date=period_end)

    payee = next(p for p in report.payees if p.contact_id == contact_id)
    assert payee.total_incl_gst == Decimal("25000.00")
    assert payee.abn == "51824753556"


@pytest.mark.asyncio
async def test_tpar_excludes_out_of_period_bills() -> None:
    cid, acct, gst = await _ctx()
    contact_id = await _make_contact("Period Boundary Subbie", cid, is_tpar=True)

    period_start = date(2029, 7, 1)
    period_end = date(2030, 6, 30)

    # Bill outside period
    await _post_bill(cid, contact_id, acct, gst, issue_date=date(2026, 6, 1), total=Decimal("50000.00"))
    # Bill inside period
    await _post_bill(cid, contact_id, acct, gst, issue_date=date(2029, 8, 1), total=Decimal("1000.00"))

    async with AsyncSessionLocal() as session:
        report = await svc.tpar_report(session, cid, from_date=period_start, to_date=period_end)

    payee = next((p for p in report.payees if p.contact_id == contact_id), None)
    assert payee is not None
    assert payee.total_incl_gst == Decimal("1000.00")


# ------------------------------------------------------------------ #
# Router tests                                                         #
# ------------------------------------------------------------------ #


