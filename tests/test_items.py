"""Tests for Batch GG/3 inventory v1.

Scope:

1. Pure WAC math in ``compute_new_wac`` — blend formula + edge cases.
2. CRUD round-trip — create, update (whitelist), archive (only when empty).
3. ``receive_stock`` updates on_hand + WAC; ``issue_stock`` returns
   COGS value at current WAC and decrements on_hand.
4. Bill integration — posting a bill with an item line receives stock,
   sets inventory_account override, and the journal balances.
5. Invoice integration — posting an invoice with an item line issues
   stock at current WAC, auto-posts Dr COGS / Cr Inventory, and the
   full journal balances.
6. Over-issue raises (strict v1 policy).
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal, Base
from saebooks.models.account import Account
from saebooks.models.bill import Bill
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.document_counter import DocumentCounter
from saebooks.models.invoice import Invoice
from saebooks.models.item import CostMethod, Item
from saebooks.models.journal import JournalLine
from saebooks.services import bills as bill_svc
from saebooks.services import invoices as inv_svc
from saebooks.services import items as items_svc

# ------------------------------------------------------------------ #
# Test DB prep                                                       #
# ------------------------------------------------------------------ #


_COUNTER_PREFIXES = {"invoice": "INV-", "bill": "BILL-"}


async def _fast_forward_counter(kind: str, model_cls: type[Base]) -> None:
    """Advance the DocumentCounter past any existing numbers so a test
    run against the persistent dev DB never hits a duplicate-number
    unique-constraint violation.
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
async def _prep() -> AsyncGenerator[None, None]:
    await _fast_forward_counter("invoice", Invoice)
    await _fast_forward_counter("bill", Bill)
    yield


# ------------------------------------------------------------------ #
# Context helper                                                     #
# ------------------------------------------------------------------ #


async def _ctx() -> tuple[
    uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID
]:
    """Return (company_id, customer_id, inventory_acct, cogs_acct, income_acct)."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None
        inv = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.code == "1-1330",  # Trading Stock on Hand
                )
            )
        ).scalar_one()
        cogs = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.code == "5-0000",  # Cost of sales
                )
            )
        ).scalar_one()
        income = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.code == "4-2000",  # Wholesale Sales
                )
            )
        ).scalar_one()

        existing = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == company.id,
                    Contact.name == "Inventory Test Ltd",
                )
            )
        ).scalars().first()
        if existing is None:
            contact = Contact(
                company_id=company.id,
                name="Inventory Test Ltd",
                contact_type=ContactType.CUSTOMER,
                email="inv@example.com",
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)
        else:
            contact = existing

        return (company.id, contact.id, inv.id, cogs.id, income.id)


async def _make_item(sku: str, *, on_hand: Decimal = Decimal("0"),
                    wac: Decimal = Decimal("0")) -> Item:
    company_id, _, inv_id, cogs_id, income_id = await _ctx()
    async with AsyncSessionLocal() as session:
        return await items_svc.create(
            session,
            company_id,
            sku=sku,
            name=f"Test item {sku}",
            inventory_account_id=inv_id,
            cogs_account_id=cogs_id,
            income_account_id=income_id,
            on_hand_qty=on_hand,
            wac_cost=wac,
        )


# ------------------------------------------------------------------ #
# Pure WAC math                                                      #
# ------------------------------------------------------------------ #


def test_compute_new_wac_first_receipt_sets_cost() -> None:
    # Empty item: receive 10 @ 5 → WAC = 5
    new = items_svc.compute_new_wac(
        old_on_hand=Decimal("0"),
        old_wac=Decimal("0"),
        received_qty=Decimal("10"),
        received_unit_cost=Decimal("5"),
    )
    assert new == Decimal("5.0000")


def test_compute_new_wac_blend() -> None:
    # Start with 10 @ 5 (value 50). Receive 10 @ 7 (value 70).
    # Total: 20 @ 120/20 = 6
    new = items_svc.compute_new_wac(
        old_on_hand=Decimal("10"),
        old_wac=Decimal("5"),
        received_qty=Decimal("10"),
        received_unit_cost=Decimal("7"),
    )
    assert new == Decimal("6.0000")


def test_compute_new_wac_rounds_to_4dp() -> None:
    # Start with 3 @ 10 (value 30). Receive 1 @ 11 (value 11).
    # Total: 4 @ 41/4 = 10.25 exact
    new = items_svc.compute_new_wac(
        old_on_hand=Decimal("3"),
        old_wac=Decimal("10"),
        received_qty=Decimal("1"),
        received_unit_cost=Decimal("11"),
    )
    assert new == Decimal("10.2500")


def test_compute_new_wac_repeating_decimal() -> None:
    # 1 @ 10 + 1 @ 11 → 21/2 = 10.5000 exact. Then add 1 @ 0 → 21/3
    # = 7.0000 exact. Add 1 @ 5 → 26/4 = 6.5000 exact. Use something
    # truly non-terminating: 1 @ 1 + 1 @ 2 → 3/2 = 1.5000 exact; boost
    # with weird: 7/3 blend → (1*10 + 2*10/3) etc. Easier: just check
    # a known rounding case:
    # old 3 @ 10 value 30; add 4 @ 1 value 4 → 34/7 = 4.857142857…
    new = items_svc.compute_new_wac(
        old_on_hand=Decimal("3"),
        old_wac=Decimal("10"),
        received_qty=Decimal("4"),
        received_unit_cost=Decimal("1"),
    )
    assert new == Decimal("4.8571")  # HALF_UP at 4th dp


def test_compute_new_wac_rejects_negatives() -> None:
    with pytest.raises(items_svc.ItemError):
        items_svc.compute_new_wac(
            old_on_hand=Decimal("0"),
            old_wac=Decimal("0"),
            received_qty=Decimal("-1"),
            received_unit_cost=Decimal("5"),
        )
    with pytest.raises(items_svc.ItemError):
        items_svc.compute_new_wac(
            old_on_hand=Decimal("0"),
            old_wac=Decimal("0"),
            received_qty=Decimal("1"),
            received_unit_cost=Decimal("-5"),
        )


# ------------------------------------------------------------------ #
# CRUD                                                                #
# ------------------------------------------------------------------ #


async def test_create_and_get_round_trip() -> None:
    item = await _make_item(f"WGT-CRUD-{uuid.uuid4().hex[:6]}")
    assert item.on_hand_qty == Decimal("0")
    assert item.wac_cost == Decimal("0")
    assert item.cost_method == CostMethod.WAC

    async with AsyncSessionLocal() as session:
        fetched = await items_svc.get(session, item.id)
    assert fetched is not None
    assert fetched.sku == item.sku


async def test_create_rejects_non_wac_cost_method() -> None:
    company_id, _, inv_id, cogs_id, income_id = await _ctx()
    async with AsyncSessionLocal() as session:
        # Pass a bogus str through — bypasses Python enum but test
        # exercises the runtime guard.
        bad = "FIFO"
        with pytest.raises(items_svc.ItemError):
            await items_svc.create(
                session,
                company_id,
                sku=f"WGT-BADCM-{uuid.uuid4().hex[:6]}",
                name="Bad",
                inventory_account_id=inv_id,
                cogs_account_id=cogs_id,
                income_account_id=income_id,
                cost_method=bad,  # type: ignore[arg-type]
            )


async def test_update_rejects_on_hand_mutation() -> None:
    item = await _make_item(f"WGT-UPD-{uuid.uuid4().hex[:6]}")
    async with AsyncSessionLocal() as session:
        with pytest.raises(items_svc.ItemError):
            await items_svc.update(session, item.id, on_hand_qty=Decimal("100"))


async def test_archive_refuses_when_stock_on_hand() -> None:
    item = await _make_item(
        f"WGT-ARCH-{uuid.uuid4().hex[:6]}",
        on_hand=Decimal("5"),
        wac=Decimal("2"),
    )
    async with AsyncSessionLocal() as session:
        with pytest.raises(items_svc.ItemError):
            await items_svc.archive(session, item.id)


async def test_archive_allowed_when_empty() -> None:
    item = await _make_item(f"WGT-ARCHOK-{uuid.uuid4().hex[:6]}")
    async with AsyncSessionLocal() as session:
        await items_svc.archive(session, item.id)
        fetched = await items_svc.get(session, item.id)
    assert fetched is not None
    assert fetched.archived_at is not None


# ------------------------------------------------------------------ #
# Stock movements (in-transaction)                                   #
# ------------------------------------------------------------------ #


async def test_receive_stock_mutates_on_hand_and_wac() -> None:
    item = await _make_item(f"WGT-RCV-{uuid.uuid4().hex[:6]}")
    async with AsyncSessionLocal() as session:
        await items_svc.receive_stock(
            session, item.id, qty=Decimal("10"), unit_cost=Decimal("4")
        )
        await session.commit()
        fetched = await items_svc.get(session, item.id)
    assert fetched is not None
    assert fetched.on_hand_qty == Decimal("10.0000")
    assert fetched.wac_cost == Decimal("4.0000")

    # Second receipt blends.
    async with AsyncSessionLocal() as session:
        await items_svc.receive_stock(
            session, item.id, qty=Decimal("10"), unit_cost=Decimal("6")
        )
        await session.commit()
        fetched = await items_svc.get(session, item.id)
    assert fetched is not None
    assert fetched.on_hand_qty == Decimal("20.0000")
    assert fetched.wac_cost == Decimal("5.0000")


async def test_issue_stock_returns_cogs_at_wac_and_decrements() -> None:
    item = await _make_item(
        f"WGT-ISS-{uuid.uuid4().hex[:6]}",
        on_hand=Decimal("10"),
        wac=Decimal("4"),
    )
    async with AsyncSessionLocal() as session:
        cogs = await items_svc.issue_stock(
            session, item.id, qty=Decimal("3")
        )
        await session.commit()
        fetched = await items_svc.get(session, item.id)
    assert cogs == Decimal("12.0000")
    assert fetched is not None
    assert fetched.on_hand_qty == Decimal("7.0000")
    assert fetched.wac_cost == Decimal("4.0000")  # unchanged on issue


async def test_issue_stock_over_on_hand_raises() -> None:
    item = await _make_item(
        f"WGT-OVER-{uuid.uuid4().hex[:6]}",
        on_hand=Decimal("3"),
        wac=Decimal("1"),
    )
    async with AsyncSessionLocal() as session:
        with pytest.raises(items_svc.ItemError):
            await items_svc.issue_stock(session, item.id, qty=Decimal("4"))


# ------------------------------------------------------------------ #
# Bill integration — posting a bill with item line receives stock    #
# ------------------------------------------------------------------ #


async def test_bill_post_receives_stock_and_balances_journal() -> None:
    company_id, contact_id, inv_id, _cogs_id, _income_id = await _ctx()
    item = await _make_item(f"WGT-BILL-{uuid.uuid4().hex[:6]}")

    # Bill 10 units @ $5 each ex-GST, no tax code (to simplify).
    # Expect: stock +10 @ WAC 5, journal Dr Inventory $50 / Cr AP $50.
    today = date.today()
    async with AsyncSessionLocal() as session:
        bill = await bill_svc.create_draft(
            session,
            company_id=company_id,
            contact_id=contact_id,
            issue_date=today,
            due_date=today + timedelta(days=30),
            lines=[
                {
                    # account_id will be overridden to item.inventory_account_id
                    "description": "Widget purchase",
                    "account_id": str(inv_id),
                    "quantity": "10",
                    "unit_price": "5",
                    "item_id": str(item.id),
                }
            ],
        )
        posted = await bill_svc.post_bill(session, bill.id, posted_by="test")
    assert posted.status.value == "POSTED"
    assert posted.base_total == Decimal("50.00")

    # Stock should be received.
    async with AsyncSessionLocal() as session:
        fetched = await items_svc.get(session, item.id)
    assert fetched is not None
    assert fetched.on_hand_qty == Decimal("10.0000")
    assert fetched.wac_cost == Decimal("5.0000")

    # Journal should balance exactly.
    async with AsyncSessionLocal() as session:
        lines = (
            await session.execute(
                select(JournalLine).where(
                    JournalLine.entry_id == posted.journal_entry_id
                )
            )
        ).scalars().all()
    debits = sum((ln.debit for ln in lines), Decimal("0"))
    credits = sum((ln.credit for ln in lines), Decimal("0"))
    assert debits == Decimal("50.00")
    assert credits == Decimal("50.00")

    # The Dr line must be posted against the inventory asset account
    # (the override fired) — not against the account_id the form passed.
    dr_accounts = {ln.account_id for ln in lines if ln.debit > 0}
    assert inv_id in dr_accounts


# ------------------------------------------------------------------ #
# Invoice integration — issue stock + auto COGS                      #
# ------------------------------------------------------------------ #


async def test_invoice_post_issues_stock_and_posts_cogs() -> None:
    company_id, contact_id, inv_acct_id, cogs_acct_id, income_acct_id = await _ctx()
    # Seed the item with 20 @ $4 on-hand so we can sell 5 @ $10.
    item = await _make_item(
        f"WGT-SELL-{uuid.uuid4().hex[:6]}",
        on_hand=Decimal("20"),
        wac=Decimal("4"),
    )

    today = date.today()
    async with AsyncSessionLocal() as session:
        inv_doc = await inv_svc.create_draft(
            session,
            company_id=company_id,
            contact_id=contact_id,
            issue_date=today,
            due_date=today + timedelta(days=30),
            lines=[
                {
                    "description": "Sell 5 widgets",
                    "account_id": str(income_acct_id),  # overridden
                    "quantity": "5",
                    "unit_price": "10",
                    "item_id": str(item.id),
                }
            ],
        )
        posted = await inv_svc.post_invoice(
            session, inv_doc.id, posted_by="test"
        )
    assert posted.status.value == "POSTED"
    # Sale: 5 x $10 = $50 ex-GST, no tax code used.
    assert posted.base_total == Decimal("50.00")

    # Stock should be reduced.
    async with AsyncSessionLocal() as session:
        fetched = await items_svc.get(session, item.id)
    assert fetched is not None
    assert fetched.on_hand_qty == Decimal("15.0000")
    assert fetched.wac_cost == Decimal("4.0000")  # issue doesn't move WAC

    # Journal: Dr AR 50 / Cr Income 50 / Dr COGS 20 / Cr Inventory 20
    # → total debits 70, total credits 70
    async with AsyncSessionLocal() as session:
        lines = (
            await session.execute(
                select(JournalLine).where(
                    JournalLine.entry_id == posted.journal_entry_id
                )
            )
        ).scalars().all()
    debits = sum((ln.debit for ln in lines), Decimal("0"))
    credits = sum((ln.credit for ln in lines), Decimal("0"))
    assert debits == Decimal("70.00")
    assert credits == Decimal("70.00")

    # COGS Dr must exist against the cogs_account_id with value 20.
    cogs_lines = [
        ln for ln in lines
        if ln.account_id == cogs_acct_id and ln.debit > 0
    ]
    assert len(cogs_lines) == 1
    assert cogs_lines[0].debit == Decimal("20.00")
    # Inventory Cr of 20 against the inventory account.
    inv_cr_lines = [
        ln for ln in lines
        if ln.account_id == inv_acct_id and ln.credit > 0
    ]
    assert len(inv_cr_lines) == 1
    assert inv_cr_lines[0].credit == Decimal("20.00")


async def test_invoice_post_over_issue_raises() -> None:
    company_id, contact_id, _, _cogs_acct_id, income_acct_id = await _ctx()
    item = await _make_item(
        f"WGT-OVERSELL-{uuid.uuid4().hex[:6]}",
        on_hand=Decimal("2"),
        wac=Decimal("4"),
    )
    today = date.today()
    async with AsyncSessionLocal() as session:
        inv_doc = await inv_svc.create_draft(
            session,
            company_id=company_id,
            contact_id=contact_id,
            issue_date=today,
            due_date=today + timedelta(days=30),
            lines=[
                {
                    "description": "Sell more than on hand",
                    "account_id": str(income_acct_id),
                    "quantity": "5",
                    "unit_price": "10",
                    "item_id": str(item.id),
                }
            ],
        )
        with pytest.raises(items_svc.ItemError):
            await inv_svc.post_invoice(session, inv_doc.id, posted_by="test")
