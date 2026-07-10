"""Wave D — multi-method inventory costing (per-company setting).

Richard's decision (2): inventory costing is a PER-COMPANY setting; the
engine supports multiple methods and the client chooses. This file
proves, per method, the COGS/valuation behaviour on a receive→issue
sequence AND that the bill/invoice posting sites dispatch on the
company's ``costing_method``:

* weighted_average (default) — WAC blend on receive, COGS at the running
  average on issue. Byte-for-byte the pre-Wave-D behaviour.
* fifo — receive creates a cost layer; issue consumes layers
  oldest-first and posts COGS = sum of consumed layers.
* quantity_only — receive/issue adjust on-hand only; issue posts NO
  COGS/valuation journal.

Plus the per-company SELECTOR: two companies with different methods are
independent; a company is never forced onto one method; the default is
weighted_average.

Dedicated companies are created per method (each with its own AU CoA via
``apply_template``) so nothing mutates the shared seed company's
costing_method — avoids any cross-test / cross-worker race on shared
state. Company rows are INSERTed directly (bypassing the edition
company-cap check, same as tests/test_dutiable_events.py).
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.company import Company, CostingMethod
from saebooks.models.contact import Contact, ContactType
from saebooks.models.inventory_cost_layer import InventoryCostLayer
from saebooks.models.journal import JournalLine
from saebooks.services import bills as bill_svc
from saebooks.services import invoices as inv_svc
from saebooks.services import items as items_svc
from saebooks.services import templates as templates_svc

pytestmark = pytest.mark.postgres_only

_DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


# --------------------------------------------------------------------------- #
# Company / account helpers
# --------------------------------------------------------------------------- #
async def _make_company(method: str | None) -> uuid.UUID:
    """Create a company with the AU CoA and the given costing_method.

    ``method=None`` leaves the column at its server-default so the
    default-behaviour test can prove weighted_average is the fallback.
    """
    cid = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        company = Company(
            id=cid,
            tenant_id=_DEFAULT_TENANT,
            name=f"WD costing {method or 'default'} {cid.hex[:8]}",
            base_currency="AUD",
        )
        if method is not None:
            company.costing_method = method
        session.add(company)
        await session.flush()
        await templates_svc.apply_template(session, cid, "au/default")
        await session.commit()
    return cid


async def _accounts(company_id: uuid.UUID) -> dict[str, uuid.UUID]:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company_id,
                    Account.code.in_(["1-1330", "5-2000", "4-2000"]),
                )
            )
        ).scalars().all()
        by_code = {a.code: a.id for a in rows}
    return {
        "inventory": by_code["1-1330"],
        "cogs": by_code["5-2000"],
        "income": by_code["4-2000"],
    }


async def _customer(company_id: uuid.UUID) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        contact = Contact(
            company_id=company_id,
            tenant_id=_DEFAULT_TENANT,
            name="WD Costing Customer",
            contact_type=ContactType.CUSTOMER,
            email="wd@example.com",
        )
        session.add(contact)
        await session.commit()
        await session.refresh(contact)
        return contact.id


async def _make_item(company_id: uuid.UUID, accts: dict[str, uuid.UUID]) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        item = await items_svc.create(
            session,
            company_id,
            sku=f"WD-{uuid.uuid4().hex[:8].upper()}",
            name="WD costing item",
            inventory_account_id=accts["inventory"],
            cogs_account_id=accts["cogs"],
            income_account_id=accts["income"],
        )
        return item.id


# --------------------------------------------------------------------------- #
# Per-company selector
# --------------------------------------------------------------------------- #
async def test_selector_defaults_weighted_average() -> None:
    """A company with no explicit method resolves to weighted_average."""
    cid = await _make_company(None)
    async with AsyncSessionLocal() as session:
        method = await items_svc.get_company_costing_method(session, cid)
    assert method == CostingMethod.WEIGHTED_AVERAGE


async def test_selector_per_company_independent() -> None:
    """Two companies keep independent methods — neither is forced."""
    fifo_cid = await _make_company("fifo")
    qty_cid = await _make_company("quantity_only")
    async with AsyncSessionLocal() as session:
        assert (
            await items_svc.get_company_costing_method(session, fifo_cid)
            == CostingMethod.FIFO
        )
        assert (
            await items_svc.get_company_costing_method(session, qty_cid)
            == CostingMethod.QUANTITY_ONLY
        )


async def test_selector_unknown_method_rejected() -> None:
    """The companies service refuses an out-of-set costing_method."""
    from saebooks.services import companies as companies_svc

    cid = await _make_company(None)
    async with AsyncSessionLocal() as session:
        company = await session.get(Company, cid)
        assert company is not None
        with pytest.raises(ValueError, match="costing_method"):
            await companies_svc.update(
                session, cid, costing_method="lifo",
                expected_version=company.version,
            )


# --------------------------------------------------------------------------- #
# Service-layer method behaviour (explicit method=, deterministic)
# --------------------------------------------------------------------------- #
async def test_wac_receive_blend_and_issue_cogs() -> None:
    """weighted_average: receive blends WAC; issue = qty * running WAC."""
    cid = await _make_company("weighted_average")
    accts = await _accounts(cid)
    item_id = await _make_item(cid, accts)

    async with AsyncSessionLocal() as session:
        await items_svc.receive_stock(
            session, item_id, qty=Decimal("10"), unit_cost=Decimal("4"),
            method=CostingMethod.WEIGHTED_AVERAGE,
        )
        await items_svc.receive_stock(
            session, item_id, qty=Decimal("10"), unit_cost=Decimal("6"),
            method=CostingMethod.WEIGHTED_AVERAGE,
        )
        await session.commit()
        item = await items_svc.get(session, item_id)
    assert item is not None
    assert item.on_hand_qty == Decimal("20.0000")
    assert item.wac_cost == Decimal("5.0000")  # (40+60)/20

    async with AsyncSessionLocal() as session:
        cogs = await items_svc.issue_stock(
            session, item_id, qty=Decimal("3"),
            method=CostingMethod.WEIGHTED_AVERAGE,
        )
        await session.commit()
        item = await items_svc.get(session, item_id)
    assert cogs == Decimal("15.0000")  # 3 * 5
    assert item is not None
    assert item.on_hand_qty == Decimal("17.0000")
    # No cost layers created for a WAC company.
    async with AsyncSessionLocal() as session:
        layers = (
            await session.execute(
                select(InventoryCostLayer).where(
                    InventoryCostLayer.item_id == item_id
                )
            )
        ).scalars().all()
    assert layers == []


async def test_wac_default_method_matches_explicit() -> None:
    """The default ``method`` kwarg is weighted_average (unchanged callers)."""
    cid = await _make_company("weighted_average")
    accts = await _accounts(cid)
    item_id = await _make_item(cid, accts)
    async with AsyncSessionLocal() as session:
        await items_svc.receive_stock(
            session, item_id, qty=Decimal("10"), unit_cost=Decimal("4"),
        )  # no method= → defaults to WAC
        await session.commit()
        cogs = await items_svc.issue_stock(session, item_id, qty=Decimal("2"))
        await session.commit()
    assert cogs == Decimal("8.0000")  # 2 * 4, WAC default


async def test_fifo_consumes_oldest_layers_first() -> None:
    """fifo: issue consumes layers oldest-first; COGS = consumed layers."""
    cid = await _make_company("fifo")
    accts = await _accounts(cid)
    item_id = await _make_item(cid, accts)

    async with AsyncSessionLocal() as session:
        await items_svc.receive_stock(
            session, item_id, qty=Decimal("10"), unit_cost=Decimal("4"),
            method=CostingMethod.FIFO, received_date=date(2026, 1, 1),
        )
        await items_svc.receive_stock(
            session, item_id, qty=Decimal("10"), unit_cost=Decimal("6"),
            method=CostingMethod.FIFO, received_date=date(2026, 2, 1),
        )
        await session.commit()

    # Issue 15 → 10@4 (=40) from layer 1 + 5@6 (=30) from layer 2 = 70.
    async with AsyncSessionLocal() as session:
        cogs = await items_svc.issue_stock(
            session, item_id, qty=Decimal("15"), method=CostingMethod.FIFO,
        )
        await session.commit()
        item = await items_svc.get(session, item_id)
    assert cogs == Decimal("70.0000")
    assert item is not None
    assert item.on_hand_qty == Decimal("5.0000")

    # Layer 1 fully consumed (remaining 0), layer 2 has 5 left.
    async with AsyncSessionLocal() as session:
        layers = (
            await session.execute(
                select(InventoryCostLayer)
                .where(InventoryCostLayer.item_id == item_id)
                .order_by(InventoryCostLayer.received_date)
            )
        ).scalars().all()
    assert len(layers) == 2
    assert layers[0].unit_cost == Decimal("4.0000")
    assert layers[0].remaining_qty == Decimal("0.0000")
    assert layers[1].unit_cost == Decimal("6.0000")
    assert layers[1].remaining_qty == Decimal("5.0000")


async def test_fifo_oldest_by_date_not_insertion_order() -> None:
    """fifo consumption is ordered by received_date, not insert order."""
    cid = await _make_company("fifo")
    accts = await _accounts(cid)
    item_id = await _make_item(cid, accts)

    async with AsyncSessionLocal() as session:
        # Insert the NEWER receipt first, the OLDER (cheaper) one second.
        await items_svc.receive_stock(
            session, item_id, qty=Decimal("5"), unit_cost=Decimal("9"),
            method=CostingMethod.FIFO, received_date=date(2026, 5, 1),
        )
        await items_svc.receive_stock(
            session, item_id, qty=Decimal("5"), unit_cost=Decimal("3"),
            method=CostingMethod.FIFO, received_date=date(2026, 1, 1),
        )
        await session.commit()

    # Issue 5 → must take the older 5@3 (=15), not the newer 5@9.
    async with AsyncSessionLocal() as session:
        cogs = await items_svc.issue_stock(
            session, item_id, qty=Decimal("5"), method=CostingMethod.FIFO,
        )
        await session.commit()
    assert cogs == Decimal("15.0000")


async def test_quantity_only_tracks_qty_no_cogs() -> None:
    """quantity_only: on-hand moves; issue returns 0; wac_cost untouched."""
    cid = await _make_company("quantity_only")
    accts = await _accounts(cid)
    item_id = await _make_item(cid, accts)

    async with AsyncSessionLocal() as session:
        await items_svc.receive_stock(
            session, item_id, qty=Decimal("10"), unit_cost=Decimal("4"),
            method=CostingMethod.QUANTITY_ONLY,
        )
        await items_svc.receive_stock(
            session, item_id, qty=Decimal("10"), unit_cost=Decimal("6"),
            method=CostingMethod.QUANTITY_ONLY,
        )
        await session.commit()
        item = await items_svc.get(session, item_id)
    assert item is not None
    assert item.on_hand_qty == Decimal("20.0000")
    assert item.wac_cost == Decimal("0.0000")  # never valued

    async with AsyncSessionLocal() as session:
        cogs = await items_svc.issue_stock(
            session, item_id, qty=Decimal("5"),
            method=CostingMethod.QUANTITY_ONLY,
        )
        await session.commit()
        item = await items_svc.get(session, item_id)
    assert cogs == Decimal("0")
    assert item is not None
    assert item.on_hand_qty == Decimal("15.0000")
    # No cost layers under quantity_only.
    async with AsyncSessionLocal() as session:
        layers = (
            await session.execute(
                select(InventoryCostLayer).where(
                    InventoryCostLayer.item_id == item_id
                )
            )
        ).scalars().all()
    assert layers == []


# --------------------------------------------------------------------------- #
# End-to-end dispatch through the bill/invoice posting sites
# --------------------------------------------------------------------------- #
async def _post_bill_receive(
    company_id: uuid.UUID, contact_id: uuid.UUID, item_id: uuid.UUID,
    inv_acct: uuid.UUID, *, qty: str, unit_price: str,
) -> None:
    today = date.today()
    async with AsyncSessionLocal() as session:
        bill = await bill_svc.create_draft(
            session,
            company_id=company_id,
            contact_id=contact_id,
            issue_date=today,
            due_date=today + timedelta(days=30),
            lines=[{
                "description": "WD receive",
                "account_id": str(inv_acct),
                "quantity": qty,
                "unit_price": unit_price,
                "item_id": str(item_id),
            }],
        )
        await bill_svc.post_bill(session, bill.id, posted_by="test")


async def _post_invoice_sell(
    company_id: uuid.UUID, contact_id: uuid.UUID, item_id: uuid.UUID,
    income_acct: uuid.UUID, *, qty: str, unit_price: str,
) -> uuid.UUID:
    today = date.today()
    async with AsyncSessionLocal() as session:
        inv_doc = await inv_svc.create_draft(
            session,
            company_id=company_id,
            contact_id=contact_id,
            issue_date=today,
            due_date=today + timedelta(days=30),
            lines=[{
                "description": "WD sell",
                "account_id": str(income_acct),
                "quantity": qty,
                "unit_price": unit_price,
                "item_id": str(item_id),
            }],
        )
        posted = await inv_svc.post_invoice(session, inv_doc.id, posted_by="test")
        return posted.journal_entry_id


async def _journal_totals(entry_id: uuid.UUID) -> tuple[Decimal, Decimal, list]:
    async with AsyncSessionLocal() as session:
        lines = (
            await session.execute(
                select(JournalLine).where(JournalLine.entry_id == entry_id)
            )
        ).scalars().all()
    debits = sum((ln.debit for ln in lines), Decimal("0"))
    credits = sum((ln.credit for ln in lines), Decimal("0"))
    return debits, credits, list(lines)


async def test_e2e_fifo_dispatch_from_posting_sites() -> None:
    """Posting a bill then an invoice on a FIFO company consumes layers."""
    cid = await _make_company("fifo")
    accts = await _accounts(cid)
    contact_id = await _customer(cid)
    item_id = await _make_item(cid, accts)

    # Receive 10 @ $5 (creates a FIFO layer via bills.py dispatch).
    await _post_bill_receive(
        cid, contact_id, item_id, accts["inventory"], qty="10", unit_price="5"
    )
    async with AsyncSessionLocal() as session:
        layers = (
            await session.execute(
                select(InventoryCostLayer).where(
                    InventoryCostLayer.item_id == item_id
                )
            )
        ).scalars().all()
    assert len(layers) == 1
    assert layers[0].remaining_qty == Decimal("10.0000")
    assert layers[0].unit_cost == Decimal("5.0000")

    # Sell 5 @ $10 → COGS 5@5 = 25 from the layer (invoices.py dispatch).
    entry_id = await _post_invoice_sell(
        cid, contact_id, item_id, accts["income"], qty="5", unit_price="10"
    )
    debits, credits, lines = await _journal_totals(entry_id)
    # Dr AR 50 / Cr Income 50 / Dr COGS 25 / Cr Inventory 25 = 75 / 75.
    assert debits == Decimal("75.00")
    assert credits == Decimal("75.00")
    cogs_lines = [ln for ln in lines if ln.account_id == accts["cogs"] and ln.debit > 0]
    assert len(cogs_lines) == 1
    assert cogs_lines[0].debit == Decimal("25.00")

    async with AsyncSessionLocal() as session:
        layer = (
            await session.execute(
                select(InventoryCostLayer).where(
                    InventoryCostLayer.item_id == item_id
                )
            )
        ).scalars().first()
    assert layer is not None
    assert layer.remaining_qty == Decimal("5.0000")


async def test_e2e_quantity_only_posts_no_cogs() -> None:
    """A quantity_only company posts NO COGS/valuation journal on sale."""
    cid = await _make_company("quantity_only")
    accts = await _accounts(cid)
    contact_id = await _customer(cid)
    item_id = await _make_item(cid, accts)

    await _post_bill_receive(
        cid, contact_id, item_id, accts["inventory"], qty="10", unit_price="5"
    )
    # No cost layer, wac untouched.
    async with AsyncSessionLocal() as session:
        item = await items_svc.get(session, item_id)
        layers = (
            await session.execute(
                select(InventoryCostLayer).where(
                    InventoryCostLayer.item_id == item_id
                )
            )
        ).scalars().all()
    assert item is not None
    assert item.on_hand_qty == Decimal("10.0000")
    assert item.wac_cost == Decimal("0.0000")
    assert layers == []

    entry_id = await _post_invoice_sell(
        cid, contact_id, item_id, accts["income"], qty="5", unit_price="10"
    )
    debits, credits, lines = await _journal_totals(entry_id)
    # Only Dr AR 50 / Cr Income 50 — no COGS, no Inventory line.
    assert debits == Decimal("50.00")
    assert credits == Decimal("50.00")
    assert all(ln.account_id != accts["cogs"] for ln in lines)
    assert all(ln.account_id != accts["inventory"] for ln in lines)

    async with AsyncSessionLocal() as session:
        item = await items_svc.get(session, item_id)
    assert item is not None
    assert item.on_hand_qty == Decimal("5.0000")


async def test_e2e_wac_default_unchanged() -> None:
    """A default (weighted_average) company keeps the pre-Wave-D behaviour."""
    cid = await _make_company("weighted_average")
    accts = await _accounts(cid)
    contact_id = await _customer(cid)
    item_id = await _make_item(cid, accts)

    await _post_bill_receive(
        cid, contact_id, item_id, accts["inventory"], qty="10", unit_price="4"
    )
    entry_id = await _post_invoice_sell(
        cid, contact_id, item_id, accts["income"], qty="5", unit_price="10"
    )
    debits, credits, lines = await _journal_totals(entry_id)
    # Dr AR 50 / Cr Income 50 / Dr COGS 20 (5@4) / Cr Inventory 20 = 70/70.
    assert debits == Decimal("70.00")
    assert credits == Decimal("70.00")
    cogs_lines = [ln for ln in lines if ln.account_id == accts["cogs"] and ln.debit > 0]
    assert len(cogs_lines) == 1
    assert cogs_lines[0].debit == Decimal("20.00")
    # No cost layers for a WAC company even through the posting path.
    async with AsyncSessionLocal() as session:
        layers = (
            await session.execute(
                select(InventoryCostLayer).where(
                    InventoryCostLayer.item_id == item_id
                )
            )
        ).scalars().all()
    assert layers == []
