"""Tests for the Batch GG/2 foreign-currency pipeline.

Scope:

1.  Pure-math unit tests for ``apply_document_fx`` + ``compute_realised_fx``.
2.  ``get_rate`` cache behaviour — identity short-circuit, direct-pair hit,
    inverse-pair hit, miss-fetches-and-caches via a registered fake fetcher.
3.  Integration: an invoice booked at one USD→AUD rate is settled at a
    different rate and produces the expected realised FX gain or loss.
4.  Integration: OUTGOING bill-side FX flow mirrors the invoice path with
    the opposite sign convention.
5.  Cross-currency allocation is rejected at allocate() time (out of
    scope for v1).
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal, Base
from saebooks.models.account import Account
from saebooks.models.bill import Bill
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.document_counter import DocumentCounter
from saebooks.models.fx_rate_snapshot import FxRateSnapshot
from saebooks.models.invoice import Invoice
from saebooks.models.journal import JournalEntry, JournalLine
from saebooks.models.payment import Payment, PaymentDirection
from saebooks.models.tax_code import TaxCode
from saebooks.services import bills as bill_svc
from saebooks.services import invoices as inv_svc
from saebooks.services import payments as pay_svc
from saebooks.services.fx import (
    apply_document_fx,
    compute_realised_fx,
    get_rate,
)
from saebooks.services.fx import rates as fx_rates
pytestmark = pytest.mark.postgres_only

# ------------------------------------------------------------------ #
# Test DB prep                                                       #
# ------------------------------------------------------------------ #


_COUNTER_PREFIXES = {
    "invoice": "INV-",
    "bill": "BILL-",
    "payment": "PAY-",
    "credit_note": "CN-",
}


async def _fast_forward_counter(kind: str, model_cls: type[Base]) -> None:
    """Advance the ``DocumentCounter`` for ``kind`` past every existing
    document number. Same pattern as ``tests/test_payments.py`` — the
    persistent dev DB accumulates rows across runs.
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
    await _fast_forward_counter("payment", Payment)
    yield


@pytest.fixture(autouse=True)
async def _isolate_fx_state() -> AsyncGenerator[None, None]:
    """Snapshot the fetcher registry + FX snapshot rows around each test.

    FX rate snapshots are global (deliberately NOT CompanyScoped) so if one
    test writes a USD→AUD snapshot it will bleed into the next. Clear
    matching synthetic rows before + after each test, and reset the
    fetcher registry so a test-local fake never leaks into the next test.
    """
    saved = dict(fx_rates._FETCHERS)
    fx_rates.clear_fetchers()
    async with AsyncSessionLocal() as session:
        await session.execute(
            sa_delete(FxRateSnapshot).where(FxRateSnapshot.source == "fake")
        )
        await session.commit()
    try:
        yield
    finally:
        fx_rates.clear_fetchers()
        for src, fn in saved.items():
            fx_rates.register_fetcher(src, fn)
        async with AsyncSessionLocal() as session:
            await session.execute(
                sa_delete(FxRateSnapshot).where(FxRateSnapshot.source == "fake")
            )
            await session.commit()


# ------------------------------------------------------------------ #
# Context helper                                                      #
# ------------------------------------------------------------------ #


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company, contact, bank_id, income_acct_id, expense_acct_id, gst_tc_id)."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
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
        expense = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.code == "6-1000",  # Advertising — any EXPENSE will do
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
                    Contact.name == "Test FX Ltd",
                )
            )
        ).scalars().first()
        if existing is None:
            contact = Contact(
                company_id=company.id,
                name="Test FX Ltd",
                contact_type=ContactType.CUSTOMER,
                email="fx@example.com",
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)
        else:
            contact = existing

        return (
            company.id,
            contact.id,
            bank.id,
            income.id,
            expense.id,
            gst.id,
        )


async def _journal_lines(journal_entry_id: uuid.UUID) -> list[JournalLine]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(JournalLine).where(
                JournalLine.entry_id == journal_entry_id
            )
        )
        return list(result.scalars().all())


async def _account_code(account_id: uuid.UUID) -> str:
    async with AsyncSessionLocal() as session:
        acct = await session.get(Account, account_id)
        assert acct is not None
        return acct.code


# ------------------------------------------------------------------ #
# Pure math                                                          #
# ------------------------------------------------------------------ #


def test_apply_document_fx_identity_when_rate_is_one() -> None:
    out = apply_document_fx(
        subtotal=Decimal("1000.00"),
        tax_total=Decimal("100.00"),
        total=Decimal("1100.00"),
        fx_rate=Decimal("1"),
    )
    assert out.base_subtotal == Decimal("1000.00")
    assert out.base_tax_total == Decimal("100.00")
    assert out.base_total == Decimal("1100.00")


def test_apply_document_fx_translates_to_base() -> None:
    # USD invoice of $1,100 at 1.50 AUD/USD → $1,650 AUD
    out = apply_document_fx(
        subtotal=Decimal("1000.00"),
        tax_total=Decimal("100.00"),
        total=Decimal("1100.00"),
        fx_rate=Decimal("1.50"),
    )
    assert out.base_subtotal == Decimal("1500.00")
    assert out.base_tax_total == Decimal("150.00")
    assert out.base_total == Decimal("1650.00")


def test_compute_realised_fx_gain() -> None:
    r = compute_realised_fx(
        alloc_amount=Decimal("1000.00"),
        document_rate=Decimal("1.40"),
        payment_rate=Decimal("1.50"),
    )
    # AR was booked at 1.40 (1400 AUD) but payment brought in 1500 AUD → +100 gain
    assert r.alloc_base_at_document_rate == Decimal("1400.00")
    assert r.alloc_base_at_payment_rate == Decimal("1500.00")
    assert r.delta == Decimal("100.00")
    assert r.is_gain is True
    assert r.is_zero is False


def test_compute_realised_fx_loss() -> None:
    r = compute_realised_fx(
        alloc_amount=Decimal("1000.00"),
        document_rate=Decimal("1.50"),
        payment_rate=Decimal("1.40"),
    )
    assert r.delta == Decimal("-100.00")
    assert r.is_gain is False
    assert r.is_zero is False


def test_compute_realised_fx_zero_when_rates_match() -> None:
    r = compute_realised_fx(
        alloc_amount=Decimal("500.00"),
        document_rate=Decimal("1.45"),
        payment_rate=Decimal("1.45"),
    )
    assert r.delta == Decimal("0.00")
    assert r.is_gain is False
    assert r.is_zero is True


# ------------------------------------------------------------------ #
# get_rate cache behaviour                                            #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_get_rate_identity_short_circuits() -> None:
    async with AsyncSessionLocal() as session:
        rate = await get_rate(
            session,
            from_ccy="AUD",
            to_ccy="AUD",
            as_of=date(2026, 4, 20),
        )
    assert rate == Decimal("1")


@pytest.mark.asyncio
async def test_get_rate_miss_invokes_fetcher_and_caches() -> None:
    calls: list[tuple[str, str, date]] = []

    async def fake(from_ccy: str, to_ccy: str, as_of: date) -> Decimal:
        calls.append((from_ccy, to_ccy, as_of))
        return Decimal("1.50")

    fx_rates.register_fetcher("fake", fake)

    async with AsyncSessionLocal() as session:
        r1 = await get_rate(
            session,
            from_ccy="USD",
            to_ccy="AUD",
            as_of=date(2026, 4, 20),
            source="fake",
        )
        # ``fetch_and_cache`` flushes the snapshot — we must commit so a
        # fresh session in the next step sees it. In production this
        # commit rides on the parent transaction (invoice post / payment
        # post / bill post).
        await session.commit()
    # Second call on same day should hit cache, NOT re-invoke fetcher.
    async with AsyncSessionLocal() as session:
        r2 = await get_rate(
            session,
            from_ccy="USD",
            to_ccy="AUD",
            as_of=date(2026, 4, 20),
            source="fake",
        )
    assert r1 == Decimal("1.50")
    assert r2 == Decimal("1.50")
    assert len(calls) == 1, "Fetcher should only be called on cache miss"


@pytest.mark.asyncio
async def test_get_rate_inverse_pair_hit() -> None:
    async def fake(from_ccy: str, to_ccy: str, as_of: date) -> Decimal:
        return Decimal("0.6666")  # AUD→USD e.g. 1/1.50

    fx_rates.register_fetcher("fake", fake)

    async with AsyncSessionLocal() as session:
        # Seed cache with AUD→USD
        aud_to_usd = await get_rate(
            session,
            from_ccy="AUD",
            to_ccy="USD",
            as_of=date(2026, 4, 20),
            source="fake",
        )
        await session.commit()  # Persist the snapshot row.
    assert aud_to_usd == Decimal("0.6666")

    # Request inverse pair — should derive from the cached AUD→USD row
    # WITHOUT calling the fetcher again. We swap the fetcher out for one
    # that would trip the test if called.
    fx_rates.clear_fetchers()

    async def boom(from_ccy: str, to_ccy: str, as_of: date) -> Decimal:
        raise AssertionError("Fetcher must not be called — inverse cache should hit")

    fx_rates.register_fetcher("fake", boom)

    async with AsyncSessionLocal() as session:
        usd_to_aud = await get_rate(
            session,
            from_ccy="USD",
            to_ccy="AUD",
            as_of=date(2026, 4, 20),
            source="fake",
        )
    # 1 / 0.6666 ≈ 1.50015... (the inverse formula quantises to 8dp)
    assert usd_to_aud > Decimal("1.499")
    assert usd_to_aud < Decimal("1.501")


@pytest.mark.asyncio
async def test_get_rate_raises_when_no_fetcher_registered() -> None:
    # Registry cleared by isolation fixture; no cache row for GBP→AUD.
    async with AsyncSessionLocal() as session:
        with pytest.raises(fx_rates.FxRateError, match="No fetcher"):
            await get_rate(
                session,
                from_ccy="GBP",
                to_ccy="AUD",
                as_of=date(2026, 4, 20),
                source="fake",
            )


# ------------------------------------------------------------------ #
# Invoice + payment end-to-end                                        #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_foreign_invoice_post_translates_to_base() -> None:
    """USD invoice posted at 1.50 USD→AUD should Dr AR in AUD base."""
    cid, contact, _bank, income, _exp, gst = await _ctx()
    today = date(2026, 4, 20)
    async with AsyncSessionLocal() as session:
        inv = await inv_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=today,
            due_date=today + timedelta(days=30),
            currency="USD",
            fx_rate=Decimal("1.50"),
            lines=[
                {
                    "description": "US consulting",
                    "account_id": income,
                    "tax_code_id": gst,
                    "quantity": Decimal("10"),
                    "unit_price": Decimal("100"),
                    "discount_pct": Decimal("0"),
                },
            ],
        )

    assert inv.currency == "USD"
    assert inv.fx_rate == Decimal("1.50")
    # USD totals: subtotal $1,000, GST $100, total $1,100
    assert inv.subtotal == Decimal("1000.00")
    assert inv.tax_total == Decimal("100.00")
    assert inv.total == Decimal("1100.00")
    # AUD base totals: $1,500 / $150 / $1,650
    assert inv.base_subtotal == Decimal("1500.00")
    assert inv.base_tax_total == Decimal("150.00")
    assert inv.base_total == Decimal("1650.00")

    async with AsyncSessionLocal() as session:
        posted = await inv_svc.post_invoice(session, inv.id, posted_by="test")

    # Journal balances at AUD base total; Dr AR == base_total.
    lines = await _journal_lines(posted.journal_entry_id)  # type: ignore[arg-type]
    total_dr = sum((ln.debit for ln in lines), Decimal("0"))
    total_cr = sum((ln.credit for ln in lines), Decimal("0"))
    assert total_dr == total_cr
    assert total_dr == Decimal("1650.00"), (
        f"Expected Dr AR base_total 1650.00, got {total_dr}; lines: "
        f"{[(ln.debit, ln.credit) for ln in lines]}"
    )


@pytest.mark.asyncio
async def test_foreign_invoice_gain_on_higher_payment_rate() -> None:
    """USD invoice @ 1.40, USD receipt @ 1.50 → realised gain ($100 base)."""
    cid, contact, bank, income, _exp, gst = await _ctx()
    today = date(2026, 4, 20)

    async with AsyncSessionLocal() as session:
        inv = await inv_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=today,
            due_date=today + timedelta(days=30),
            currency="USD",
            fx_rate=Decimal("1.40"),
            lines=[
                {
                    "description": "Fee",
                    "account_id": income,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("1000"),
                    "discount_pct": Decimal("0"),
                },
            ],
        )
    async with AsyncSessionLocal() as session:
        await inv_svc.post_invoice(session, inv.id, posted_by="test")

    async with AsyncSessionLocal() as session:
        pay = await pay_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            bank_account_id=bank,
            payment_date=today + timedelta(days=7),
            amount=Decimal("1100.00"),
            direction=PaymentDirection.INCOMING,
            currency="USD",
            fx_rate=Decimal("1.50"),
        )
    async with AsyncSessionLocal() as session:
        await pay_svc.allocate(
            session,
            pay.id,
            invoice_allocations=[(inv.id, Decimal("1100.00"))],
        )
    async with AsyncSessionLocal() as session:
        posted = await pay_svc.post_payment(session, pay.id, posted_by="test")

    lines = await _journal_lines(posted.journal_entry_id)  # type: ignore[arg-type]
    total_dr = sum((ln.debit for ln in lines), Decimal("0"))
    total_cr = sum((ln.credit for ln in lines), Decimal("0"))
    assert total_dr == total_cr

    # Dr Bank $1,650 (USD $1,100 x 1.50); Cr AR $1,540 (USD $1,100 x 1.40);
    # plug: Cr Exchange Rate Gain $110
    by_code: dict[str, tuple[Decimal, Decimal]] = {}
    for ln in lines:
        code = await _account_code(ln.account_id)
        existing = by_code.get(code, (Decimal("0"), Decimal("0")))
        by_code[code] = (existing[0] + ln.debit, existing[1] + ln.credit)
    assert by_code["1-1110"] == (Decimal("1650.00"), Decimal("0"))
    assert by_code["1-1200"] == (Decimal("0"), Decimal("1540.00"))
    assert by_code["6-1640"] == (Decimal("0"), Decimal("110.00")), (
        f"Expected Cr FX gain 110.00, got {by_code.get('6-1640')}"
    )


@pytest.mark.asyncio
async def test_foreign_invoice_loss_on_lower_payment_rate() -> None:
    """USD invoice @ 1.50, USD receipt @ 1.40 → realised loss ($110 base)."""
    cid, contact, bank, income, _exp, gst = await _ctx()
    today = date(2026, 4, 20)

    async with AsyncSessionLocal() as session:
        inv = await inv_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=today,
            due_date=today + timedelta(days=30),
            currency="USD",
            fx_rate=Decimal("1.50"),
            lines=[
                {
                    "description": "Fee",
                    "account_id": income,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("1000"),
                    "discount_pct": Decimal("0"),
                },
            ],
        )
    async with AsyncSessionLocal() as session:
        await inv_svc.post_invoice(session, inv.id, posted_by="test")

    async with AsyncSessionLocal() as session:
        pay = await pay_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            bank_account_id=bank,
            payment_date=today + timedelta(days=7),
            amount=Decimal("1100.00"),
            direction=PaymentDirection.INCOMING,
            currency="USD",
            fx_rate=Decimal("1.40"),
        )
    async with AsyncSessionLocal() as session:
        await pay_svc.allocate(
            session,
            pay.id,
            invoice_allocations=[(inv.id, Decimal("1100.00"))],
        )
    async with AsyncSessionLocal() as session:
        posted = await pay_svc.post_payment(session, pay.id, posted_by="test")

    lines = await _journal_lines(posted.journal_entry_id)  # type: ignore[arg-type]
    by_code: dict[str, tuple[Decimal, Decimal]] = {}
    for ln in lines:
        code = await _account_code(ln.account_id)
        existing = by_code.get(code, (Decimal("0"), Decimal("0")))
        by_code[code] = (existing[0] + ln.debit, existing[1] + ln.credit)

    # Dr Bank $1,540 (pay rate); Cr AR $1,650 (inv rate); plug: Dr FX Loss $110.
    assert by_code["1-1110"] == (Decimal("1540.00"), Decimal("0"))
    assert by_code["1-1200"] == (Decimal("0"), Decimal("1650.00"))
    assert by_code["6-1630"] == (Decimal("110.00"), Decimal("0")), (
        f"Expected Dr FX loss 110.00, got {by_code.get('6-1630')}"
    )
    # Entry balances.
    total_dr = sum((ln.debit for ln in lines), Decimal("0"))
    total_cr = sum((ln.credit for ln in lines), Decimal("0"))
    assert total_dr == total_cr


@pytest.mark.asyncio
async def test_same_rate_settlement_emits_no_fx_line() -> None:
    """Invoice @ 1.45, payment @ 1.45 → no Gain/Loss line at all."""
    cid, contact, bank, income, _exp, gst = await _ctx()
    today = date(2026, 4, 20)

    async with AsyncSessionLocal() as session:
        inv = await inv_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=today,
            due_date=today + timedelta(days=30),
            currency="USD",
            fx_rate=Decimal("1.45"),
            lines=[
                {
                    "description": "Fee",
                    "account_id": income,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("200"),
                    "discount_pct": Decimal("0"),
                },
            ],
        )
    async with AsyncSessionLocal() as session:
        await inv_svc.post_invoice(session, inv.id, posted_by="test")

    async with AsyncSessionLocal() as session:
        pay = await pay_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            bank_account_id=bank,
            payment_date=today + timedelta(days=3),
            amount=Decimal("220.00"),
            direction=PaymentDirection.INCOMING,
            currency="USD",
            fx_rate=Decimal("1.45"),
        )
    async with AsyncSessionLocal() as session:
        await pay_svc.allocate(
            session,
            pay.id,
            invoice_allocations=[(inv.id, Decimal("220.00"))],
        )
    async with AsyncSessionLocal() as session:
        posted = await pay_svc.post_payment(session, pay.id, posted_by="test")

    lines = await _journal_lines(posted.journal_entry_id)  # type: ignore[arg-type]
    codes = [await _account_code(ln.account_id) for ln in lines]
    assert "6-1640" not in codes
    assert "6-1630" not in codes
    assert set(codes) == {"1-1110", "1-1200"}


# ------------------------------------------------------------------ #
# Bill / OUTGOING path                                               #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_outgoing_bill_gain_on_lower_payment_rate() -> None:
    """USD bill @ 1.50, USD payment @ 1.40 → we owed more AUD than we paid → GAIN."""
    cid, contact, bank, _income, expense, gst = await _ctx()
    today = date(2026, 4, 20)

    async with AsyncSessionLocal() as session:
        bill = await bill_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=today,
            due_date=today + timedelta(days=30),
            currency="USD",
            fx_rate=Decimal("1.50"),
            lines=[
                {
                    "description": "US supplier",
                    "account_id": expense,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("1000"),
                    "discount_pct": Decimal("0"),
                },
            ],
        )
    async with AsyncSessionLocal() as session:
        await bill_svc.post_bill(session, bill.id, posted_by="test")

    async with AsyncSessionLocal() as session:
        pay = await pay_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            bank_account_id=bank,
            payment_date=today + timedelta(days=5),
            amount=Decimal("1100.00"),
            direction=PaymentDirection.OUTGOING,
            currency="USD",
            fx_rate=Decimal("1.40"),
        )
    async with AsyncSessionLocal() as session:
        await pay_svc.allocate(
            session,
            pay.id,
            bill_allocations=[(bill.id, Decimal("1100.00"))],
        )
    async with AsyncSessionLocal() as session:
        posted = await pay_svc.post_payment(session, pay.id, posted_by="test")

    lines = await _journal_lines(posted.journal_entry_id)  # type: ignore[arg-type]
    by_code: dict[str, tuple[Decimal, Decimal]] = {}
    for ln in lines:
        code = await _account_code(ln.account_id)
        existing = by_code.get(code, (Decimal("0"), Decimal("0")))
        by_code[code] = (existing[0] + ln.debit, existing[1] + ln.credit)

    # Dr AP $1,650 (bill rate); Cr Bank $1,540 (pay rate); plug: Cr FX gain $110.
    assert by_code["2-1200"] == (Decimal("1650.00"), Decimal("0"))
    assert by_code["1-1110"] == (Decimal("0"), Decimal("1540.00"))
    assert by_code["6-1640"] == (Decimal("0"), Decimal("110.00"))
    total_dr = sum((ln.debit for ln in lines), Decimal("0"))
    total_cr = sum((ln.credit for ln in lines), Decimal("0"))
    assert total_dr == total_cr


# ------------------------------------------------------------------ #
# Cross-currency guard                                                #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_cross_currency_allocation_rejected() -> None:
    """USD invoice + EUR payment → PaymentError at allocate() time."""
    cid, contact, bank, income, _exp, gst = await _ctx()
    today = date(2026, 4, 20)

    async with AsyncSessionLocal() as session:
        inv = await inv_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=today,
            due_date=today + timedelta(days=30),
            currency="USD",
            fx_rate=Decimal("1.50"),
            lines=[
                {
                    "description": "Fee",
                    "account_id": income,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("100"),
                    "discount_pct": Decimal("0"),
                },
            ],
        )
    async with AsyncSessionLocal() as session:
        await inv_svc.post_invoice(session, inv.id, posted_by="test")

    async with AsyncSessionLocal() as session:
        pay = await pay_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            bank_account_id=bank,
            payment_date=today + timedelta(days=1),
            amount=Decimal("100.00"),
            direction=PaymentDirection.INCOMING,
            currency="EUR",
            fx_rate=Decimal("1.65"),
        )
    with pytest.raises(pay_svc.PaymentError, match="Cross-currency"):
        async with AsyncSessionLocal() as session:
            await pay_svc.allocate(
                session,
                pay.id,
                invoice_allocations=[(inv.id, Decimal("100.00"))],
            )


# ------------------------------------------------------------------ #
# Unused orphan imports sanity                                        #
# ------------------------------------------------------------------ #


def test_journal_entry_model_importable() -> None:
    """Sanity import — the FX tests don't need JournalEntry directly,
    but the fixtures touch JournalLine and the suite imports the
    module, which in turn forces JournalEntry mapping. If SQLA mapping
    misconfigures under the new FX columns the whole module fails to
    load — this test gives that a visible failure signal.
    """
    assert JournalEntry is not None
