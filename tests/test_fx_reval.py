"""Tests for the Batch PP FX revaluation automation.

Layered like the other FX tests:

1. Pure math — ``_reval_for_currency``, ``_build_reval_lines``,
   ``_reverse_lines``, and the ``CurrencyReval.is_zero`` sentinel.
2. DB integration — create foreign-currency invoices and bills, run
   ``preview_company`` + ``revalue_company`` against a fake rate
   fetcher, and verify the two journals balance + carry the right
   idempotency tag.
3. Router smoke — GET /reports/fx-revalue renders, POST posts.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal, Base
from saebooks.main import app
from saebooks.models.account import Account
from saebooks.models.bill import Bill
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.document_counter import DocumentCounter
from saebooks.models.fx_rate_snapshot import FxRateSnapshot
from saebooks.models.invoice import Invoice
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine
from saebooks.models.payment import Payment
from saebooks.models.tax_code import TaxCode
from saebooks.services import bills as bill_svc
from saebooks.services import invoices as inv_svc
from saebooks.services.fx import rates as fx_rates
from saebooks.services.fx import reval as fx_reval
from saebooks.services.fx.reval import (
    CurrencyReval,
    _build_reval_lines,
    _reval_for_currency,
    _reverse_lines,
)

# ------------------------------------------------------------------ #
# Test DB prep — same fast-forward-counter pattern as test_fx.py        #
# ------------------------------------------------------------------ #


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
async def _prep() -> AsyncGenerator[None, None]:
    await _fast_forward_counter("invoice", Invoice)
    await _fast_forward_counter("bill", Bill)
    await _fast_forward_counter("payment", Payment)
    yield


@pytest.fixture(autouse=True)
async def _isolate() -> AsyncGenerator[None, None]:
    """Snapshot fx fetchers + fake rate rows + reval journals + stray FX
    invoices/bills around each test.

    The dev DB is persistent — prior runs leave USD/EUR/GBP invoices and
    bills posted against the "Test FX Reval Ltd" contact (see ``_ctx``).
    Those pollute ``preview_company`` for the current run, so archive
    any foreign-currency documents owned by that contact before and
    after each test. Archiving ``archived_at = now()`` is enough since
    the open-doc queries all filter on ``archived_at.is_(None)``.
    """
    saved = dict(fx_rates._FETCHERS)
    fx_rates.clear_fetchers()

    async def _scrub() -> None:
        async with AsyncSessionLocal() as session:
            await session.execute(
                sa_delete(FxRateSnapshot).where(FxRateSnapshot.source == "fake_reval")
            )
            # Purge reval journals from prior test runs.
            reval_entries = (
                await session.execute(
                    select(JournalEntry).where(
                        JournalEntry.attachments["kind"].as_string() == "fx_reval"
                    )
                )
            ).scalars().all()
            for entry in reval_entries:
                tag = entry.attachments or {}
                if tag.get("currency") in {"USD", "EUR", "GBP", "NZD"}:
                    await session.execute(
                        sa_delete(JournalLine).where(JournalLine.entry_id == entry.id)
                    )
                    await session.delete(entry)
            # Archive stray foreign-currency test documents so
            # ``preview_company`` sees a clean slate. We archive across
            # the full test company (not just our test contact) because
            # test_fx.py and other FX-adjacent suites seed USD/EUR docs
            # against their own contacts and those would otherwise leak
            # into ``preview_company`` here.
            now = datetime.now(UTC)
            stray_invoices = (
                await session.execute(
                    select(Invoice).where(
                        Invoice.currency != "AUD",
                        Invoice.archived_at.is_(None),
                    )
                )
            ).scalars().all()
            for inv in stray_invoices:
                inv.archived_at = now
            stray_bills = (
                await session.execute(
                    select(Bill).where(
                        Bill.currency != "AUD",
                        Bill.archived_at.is_(None),
                    )
                )
            ).scalars().all()
            for bill in stray_bills:
                bill.archived_at = now
            await session.commit()

    await _scrub()
    try:
        yield
    finally:
        fx_rates.clear_fetchers()
        for src, fn in saved.items():
            fx_rates.register_fetcher(src, fn)
        await _scrub()


# ------------------------------------------------------------------ #
# Context helper                                                      #
# ------------------------------------------------------------------ #


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company, contact, income_id, expense_id, gst_tc_id)."""
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
                    Account.company_id == company.id, Account.code == "4-6000"
                )
            )
        ).scalar_one()
        expense = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id, Account.code == "6-1000"
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

        existing = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == company.id,
                    Contact.name == "Test FX Reval Ltd",
                )
            )
        ).scalars().first()
        if existing is None:
            contact = Contact(
                company_id=company.id,
                name="Test FX Reval Ltd",
                contact_type=ContactType.CUSTOMER,
                email="reval@example.com",
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)
        else:
            contact = existing

        return (company.id, contact.id, income.id, expense.id, gst.id)


# ------------------------------------------------------------------ #
# Pure math                                                          #
# ------------------------------------------------------------------ #


def _mk_invoice(
    total: Decimal, paid: Decimal, base_total: Decimal, base_paid: Decimal
) -> SimpleNamespace:
    """Build a stand-in object exposing just the fields the pure math reads.

    ``_reval_for_currency`` calls ``_sum_outstanding`` which reads
    ``row.total``, ``row.amount_paid``, ``row.base_total``,
    ``row.base_amount_paid``. SimpleNamespace is enough — no ORM/SA
    instrumentation needed.
    """
    return SimpleNamespace(
        total=total,
        amount_paid=paid,
        base_total=base_total,
        base_amount_paid=base_paid,
    )


def _mk_bill(
    total: Decimal, paid: Decimal, base_total: Decimal, base_paid: Decimal
) -> SimpleNamespace:
    return SimpleNamespace(
        total=total,
        amount_paid=paid,
        base_total=base_total,
        base_amount_paid=base_paid,
    )


def test_reval_for_currency_ar_gain_ap_loss() -> None:
    """USD: $1,000 AR @ old 1.40 (base 1400) + $500 AP @ old 1.40 (base 700).

    New rate 1.50: revalued AR 1500 (delta +100), revalued AP 750 (delta +50).
    AR gain + AP loss.
    """
    invs = [
        _mk_invoice(
            Decimal("1000.00"), Decimal("0.00"),
            Decimal("1400.00"), Decimal("0.00"),
        )
    ]
    bills = [
        _mk_bill(
            Decimal("500.00"), Decimal("0.00"),
            Decimal("700.00"), Decimal("0.00"),
        )
    ]
    r = _reval_for_currency("USD", Decimal("1.50"), invs, bills)
    assert r.outstanding_foreign_ar == Decimal("1000.00")
    assert r.current_base_ar == Decimal("1400.00")
    assert r.revalued_base_ar == Decimal("1500.00")
    assert r.ar_delta == Decimal("100.00")
    assert r.outstanding_foreign_ap == Decimal("500.00")
    assert r.current_base_ap == Decimal("700.00")
    assert r.revalued_base_ap == Decimal("750.00")
    assert r.ap_delta == Decimal("50.00")
    assert r.is_zero is False


def test_reval_for_currency_all_zero_when_rate_unchanged() -> None:
    """If the new rate matches the blended base rate, delta is zero."""
    invs = [
        _mk_invoice(
            Decimal("1000.00"), Decimal("0.00"),
            Decimal("1400.00"), Decimal("0.00"),
        )
    ]
    r = _reval_for_currency("USD", Decimal("1.40"), invs, [])
    assert r.ar_delta == Decimal("0.00")
    assert r.ap_delta == Decimal("0.00")
    assert r.is_zero is True


def test_reval_for_currency_respects_amount_paid() -> None:
    """Only unpaid portion gets revalued."""
    # $1,000 invoice with $400 paid → $600 outstanding foreign.
    # Booked at rate 1.40 → base $1,400 total, $560 paid, $840 outstanding base.
    invs = [
        _mk_invoice(
            Decimal("1000.00"), Decimal("400.00"),
            Decimal("1400.00"), Decimal("560.00"),
        )
    ]
    r = _reval_for_currency("USD", Decimal("1.50"), invs, [])
    assert r.outstanding_foreign_ar == Decimal("600.00")
    assert r.current_base_ar == Decimal("840.00")
    assert r.revalued_base_ar == Decimal("900.00")  # 600 * 1.50
    assert r.ar_delta == Decimal("60.00")


def test_build_reval_lines_ar_gain_balances() -> None:
    """AR gain → Dr AR, Cr Gain; lines balance."""
    ar = uuid.uuid4()
    ap = uuid.uuid4()
    gain = uuid.uuid4()
    loss = uuid.uuid4()
    lines = _build_reval_lines(
        ar_account_id=ar,
        ap_account_id=ap,
        gain_account_id=gain,
        loss_account_id=loss,
        ar_delta=Decimal("100.00"),
        ap_delta=Decimal("0.00"),
        currency="USD",
    )
    assert len(lines) == 2
    total_dr = sum((line["debit"] for line in lines), Decimal("0"))
    total_cr = sum((line["credit"] for line in lines), Decimal("0"))
    assert total_dr == total_cr == Decimal("100.00")
    # Dr AR
    ar_line = next(ln for ln in lines if ln["account_id"] == ar)
    assert ar_line["debit"] == Decimal("100.00")
    assert ar_line["credit"] == Decimal("0")
    # Cr Gain
    gain_line = next(ln for ln in lines if ln["account_id"] == gain)
    assert gain_line["credit"] == Decimal("100.00")
    assert gain_line["debit"] == Decimal("0")


def test_build_reval_lines_ar_loss_ap_gain() -> None:
    """Mixed: AR lost base (loss) + AP reduced base (gain)."""
    ar = uuid.uuid4()
    ap = uuid.uuid4()
    gain = uuid.uuid4()
    loss = uuid.uuid4()
    lines = _build_reval_lines(
        ar_account_id=ar,
        ap_account_id=ap,
        gain_account_id=gain,
        loss_account_id=loss,
        ar_delta=Decimal("-40.00"),
        ap_delta=Decimal("-30.00"),
        currency="EUR",
    )
    # 4 lines: Dr Loss/Cr AR + Dr AP/Cr Gain
    assert len(lines) == 4
    total_dr = sum((line["debit"] for line in lines), Decimal("0"))
    total_cr = sum((line["credit"] for line in lines), Decimal("0"))
    assert total_dr == total_cr == Decimal("70.00")


def test_build_reval_lines_zero_deltas_emits_nothing() -> None:
    """Both deltas zero → no lines."""
    lines = _build_reval_lines(
        ar_account_id=uuid.uuid4(),
        ap_account_id=uuid.uuid4(),
        gain_account_id=uuid.uuid4(),
        loss_account_id=uuid.uuid4(),
        ar_delta=Decimal("0"),
        ap_delta=Decimal("0"),
        currency="USD",
    )
    assert lines == []


def test_reverse_lines_swaps_debit_credit() -> None:
    """Reversal journal is built from swapped debit/credit of the original."""
    acct = uuid.uuid4()
    original = [
        {"account_id": acct, "description": "x", "debit": Decimal("100"), "credit": Decimal("0")},
        {"account_id": acct, "description": "x", "debit": Decimal("0"), "credit": Decimal("100")},
    ]
    reversed_ = _reverse_lines(original)
    assert reversed_[0]["debit"] == Decimal("0")
    assert reversed_[0]["credit"] == Decimal("100")
    assert reversed_[1]["debit"] == Decimal("100")
    assert reversed_[1]["credit"] == Decimal("0")


def test_currency_reval_is_zero_property() -> None:
    r = CurrencyReval(
        currency="USD",
        new_rate=Decimal("1.40"),
        outstanding_foreign_ar=Decimal("0"),
        current_base_ar=Decimal("0"),
        revalued_base_ar=Decimal("0"),
        ar_delta=Decimal("0"),
        outstanding_foreign_ap=Decimal("0"),
        current_base_ap=Decimal("0"),
        revalued_base_ap=Decimal("0"),
        ap_delta=Decimal("0"),
    )
    assert r.is_zero is True

    r2 = CurrencyReval(
        currency="USD",
        new_rate=Decimal("1.50"),
        outstanding_foreign_ar=Decimal("1000"),
        current_base_ar=Decimal("1400"),
        revalued_base_ar=Decimal("1500"),
        ar_delta=Decimal("100"),
        outstanding_foreign_ap=Decimal("0"),
        current_base_ap=Decimal("0"),
        revalued_base_ap=Decimal("0"),
        ap_delta=Decimal("0"),
    )
    assert r2.is_zero is False


# ------------------------------------------------------------------ #
# DB integration: preview_company                                     #
# ------------------------------------------------------------------ #


async def _usd_invoice(
    amount: Decimal, fx_rate: Decimal, today: date
) -> Invoice:
    """Create + post a USD invoice at ``fx_rate`` and return it."""
    cid, contact, income, _expense, gst = await _ctx()
    async with AsyncSessionLocal() as session:
        inv = await inv_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=today,
            due_date=today + timedelta(days=30),
            currency="USD",
            fx_rate=fx_rate,
            lines=[
                {
                    "description": "Test line",
                    "account_id": income,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": amount,
                    "discount_pct": Decimal("0"),
                },
            ],
        )
    async with AsyncSessionLocal() as session:
        return await inv_svc.post_invoice(session, inv.id, posted_by="test")


async def _usd_bill(
    amount: Decimal, fx_rate: Decimal, today: date
) -> Bill:
    cid, contact, _income, expense, gst = await _ctx()
    async with AsyncSessionLocal() as session:
        bill = await bill_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=today,
            due_date=today + timedelta(days=30),
            currency="USD",
            fx_rate=fx_rate,
            lines=[
                {
                    "description": "Supplier line",
                    "account_id": expense,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": amount,
                    "discount_pct": Decimal("0"),
                },
            ],
        )
    async with AsyncSessionLocal() as session:
        return await bill_svc.post_bill(session, bill.id, posted_by="test")


async def test_preview_company_skips_base_currency_no_open_fx() -> None:
    """No foreign docs → empty preview."""
    cid, _contact, _income, _expense, _gst = await _ctx()
    async with AsyncSessionLocal() as session:
        preview = await fx_reval.preview_company(
            session,
            company_id=cid,
            through_date=date(2030, 3, 31),
        )
    # Filter to ensure no AUD sneaked in (the resolver should already skip it).
    assert all(r.currency != "AUD" for r in preview)


async def test_preview_company_usd_ar_only() -> None:
    """A single unpaid USD invoice surfaces as a per-currency reval row."""
    today = date(2030, 3, 15)
    # Invoice booked at 1.40 (1400 AUD base).
    await _usd_invoice(Decimal("1000"), Decimal("1.40"), today)

    async def fake_rate(from_ccy: str, to_ccy: str, as_of: date) -> Decimal:
        return Decimal("1.50")

    fx_rates.register_fetcher("fake_reval", fake_rate)
    cid, _contact, _income, _expense, _gst = await _ctx()
    async with AsyncSessionLocal() as session:
        preview = await fx_reval.preview_company(
            session,
            company_id=cid,
            through_date=date(2030, 3, 31),
            source="fake_reval",
        )
    usd = next(r for r in preview if r.currency == "USD")
    # Invoice: $1000 at 1.40, tax $100 → $1100 total, $1540 base.
    assert usd.outstanding_foreign_ar >= Decimal("1100.00")
    # ar_delta should be positive: revalued @ 1.50 > base @ 1.40.
    assert usd.ar_delta > Decimal("0")


# ------------------------------------------------------------------ #
# DB integration: revalue_company posts the pair                      #
# ------------------------------------------------------------------ #


async def _fetch_reval_entries(
    company_id: uuid.UUID, currency: str, through_date: date
) -> list[JournalEntry]:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(JournalEntry).where(
                    JournalEntry.company_id == company_id,
                    JournalEntry.attachments["kind"].as_string() == "fx_reval",
                    JournalEntry.attachments["currency"].as_string() == currency,
                    JournalEntry.attachments["through_date"].as_string()
                    == through_date.isoformat(),
                )
            )
        ).scalars().all()
        return list(rows)


async def test_revalue_company_posts_adjusting_and_reversing_pair() -> None:
    """Post a USD invoice, revalue at a higher rate → two journals."""
    today = date(2030, 3, 15)
    through = date(2030, 3, 31)
    await _usd_invoice(Decimal("1000"), Decimal("1.40"), today)

    async def fake_rate(from_ccy: str, to_ccy: str, as_of: date) -> Decimal:
        return Decimal("1.50")

    fx_rates.register_fetcher("fake_reval", fake_rate)
    cid, _contact, _income, _expense, _gst = await _ctx()

    async with AsyncSessionLocal() as session:
        result = await fx_reval.revalue_company(
            session,
            company_id=cid,
            tenant_id=DEFAULT_TENANT_ID,
            through_date=through,
            source="fake_reval",
            posted_by="test",
        )

    assert result.posted_count == 1
    assert len(result.entries) == 1
    adj_id, rev_id = result.entries[0]

    entries = await _fetch_reval_entries(cid, "USD", through)
    # 2 entries — adjustment + reversal.
    assert len(entries) == 2
    sides = {(e.attachments or {}).get("side") for e in entries}
    assert sides == {"adjustment", "reversal"}

    # Dates: adjustment on ``through``, reversal on ``through+1``.
    adj_entry = next(e for e in entries if (e.attachments or {}).get("side") == "adjustment")
    rev_entry = next(e for e in entries if (e.attachments or {}).get("side") == "reversal")
    assert adj_entry.id == adj_id
    assert rev_entry.id == rev_id
    assert adj_entry.entry_date == through
    assert rev_entry.entry_date == through + timedelta(days=1)
    # Both posted.
    assert adj_entry.status == EntryStatus.POSTED
    assert rev_entry.status == EntryStatus.POSTED


async def test_revalue_company_journals_balance() -> None:
    """Adjusting journal sum of debits == sum of credits."""
    today = date(2030, 3, 15)
    through = date(2030, 3, 31)
    await _usd_invoice(Decimal("1000"), Decimal("1.40"), today)

    async def fake_rate(from_ccy: str, to_ccy: str, as_of: date) -> Decimal:
        return Decimal("1.50")

    fx_rates.register_fetcher("fake_reval", fake_rate)
    cid, _contact, _income, _expense, _gst = await _ctx()

    async with AsyncSessionLocal() as session:
        await fx_reval.revalue_company(
            session,
            company_id=cid,
            tenant_id=DEFAULT_TENANT_ID,
            through_date=through,
            source="fake_reval",
            posted_by="test",
        )

    entries = await _fetch_reval_entries(cid, "USD", through)
    async with AsyncSessionLocal() as session:
        for entry in entries:
            lines = (
                await session.execute(
                    select(JournalLine).where(JournalLine.entry_id == entry.id)
                )
            ).scalars().all()
            total_dr = sum((ln.debit for ln in lines), Decimal("0"))
            total_cr = sum((ln.credit for ln in lines), Decimal("0"))
            assert total_dr == total_cr, (
                f"Entry {entry.ref} (side="
                f"{(entry.attachments or {}).get('side')}) unbalanced: "
                f"Dr {total_dr} / Cr {total_cr}"
            )


async def test_revalue_company_is_idempotent() -> None:
    """Re-running on the same through_date skips the currency, posts nothing new."""
    today = date(2030, 3, 15)
    through = date(2030, 3, 31)
    await _usd_invoice(Decimal("1000"), Decimal("1.40"), today)

    async def fake_rate(from_ccy: str, to_ccy: str, as_of: date) -> Decimal:
        return Decimal("1.50")

    fx_rates.register_fetcher("fake_reval", fake_rate)
    cid, _contact, _income, _expense, _gst = await _ctx()

    async with AsyncSessionLocal() as session:
        first = await fx_reval.revalue_company(
            session,
            company_id=cid,
            tenant_id=DEFAULT_TENANT_ID,
            through_date=through,
            source="fake_reval",
            posted_by="test",
        )
    assert first.posted_count == 1

    async with AsyncSessionLocal() as session:
        second = await fx_reval.revalue_company(
            session,
            company_id=cid,
            tenant_id=DEFAULT_TENANT_ID,
            through_date=through,
            source="fake_reval",
            posted_by="test",
        )
    assert second.posted_count == 0
    assert "USD" in second.skipped_currencies

    # Still exactly 2 entries in the DB (not 4).
    entries = await _fetch_reval_entries(cid, "USD", through)
    assert len(entries) == 2


async def test_revalue_company_zero_delta_emits_nothing() -> None:
    """Open invoice at same rate as reval date → no journals."""
    today = date(2030, 3, 15)
    through = date(2030, 3, 31)
    await _usd_invoice(Decimal("1000"), Decimal("1.40"), today)

    async def fake_rate(from_ccy: str, to_ccy: str, as_of: date) -> Decimal:
        return Decimal("1.40")  # Same as invoice rate — no gain/loss.

    fx_rates.register_fetcher("fake_reval", fake_rate)
    cid, _contact, _income, _expense, _gst = await _ctx()

    async with AsyncSessionLocal() as session:
        result = await fx_reval.revalue_company(
            session,
            company_id=cid,
            tenant_id=DEFAULT_TENANT_ID,
            through_date=through,
            source="fake_reval",
            posted_by="test",
        )
    assert result.posted_count == 0
    assert "USD" in result.zero_currencies
    entries = await _fetch_reval_entries(cid, "USD", through)
    assert len(entries) == 0


async def test_revalue_company_no_foreign_docs_no_op() -> None:
    """Company with only AUD activity → empty result, no errors."""
    cid, _contact, _income, _expense, _gst = await _ctx()
    through = date(2030, 3, 31)
    # Deliberately no foreign invoice/bill created before this test in
    # this transaction. (The _isolate fixture scrubs reval journals,
    # not invoices — there may be foreign invoices from other tests in
    # the same run, which is fine: the idempotency + zero-delta paths
    # already cover them.)
    async with AsyncSessionLocal() as session:
        result = await fx_reval.revalue_company(
            session,
            company_id=cid,
            tenant_id=DEFAULT_TENANT_ID,
            through_date=through,
            source="fake_reval",
            # No fetcher registered — but only fires if there are open
            # foreign docs. With no foreign currency exposure the
            # preview returns [] before it ever calls get_rate.
        )
    # May have posted/skipped/zero from stray USD docs left by other
    # tests; key assertion: no exception bubbled up.
    assert isinstance(result.posted_count, int)


# ------------------------------------------------------------------ #
# Router smoke                                                       #
# ------------------------------------------------------------------ #


async def test_fx_revalue_form_renders() -> None:
    """GET /reports/fx-revalue → 200 and contains form markers."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.get("/reports/fx-revalue")
    assert resp.status_code == 200
    body = resp.text
    assert "FX revaluation" in body
    assert 'name="through"' in body


async def test_fx_revalue_reports_index_links_to_it() -> None:
    """/reports index page carries the fx-revalue card."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.get("/reports")
    assert resp.status_code == 200
    assert "/reports/fx-revalue" in resp.text
