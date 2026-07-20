"""Tests for cashbook edition — record_cashbook_entry invariants.

Coverage:
- Trial-balance invariant (debits == credits) on the auto-generated JE.
- Idempotency replay (same key returns same JE; different key creates new).
- GST handling: 2-line for non-registered, 3-line auto-posted for registered.
- Configuration errors: not in cashbook mode, missing default bank,
  non-AUD base currency.
- Category errors: unknown code, wrong-direction code, transfer code
  rejected from income/expense surface.
- Amount validation: zero/negative refused.
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
from saebooks.models.journal import EntryStatus, JournalEntry
from saebooks.services import settings as settings_svc
from saebooks.services.cashbook import (
    CashbookCategoryError,
    CashbookCurrencyError,
    CashbookError,
    CashbookNotConfigured,
    record_cashbook_entry,
)

pytestmark = pytest.mark.postgres_only


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
async def _restore_seed_company_after_module():
    """Reset the seed company back to its post-seed defaults when this
    module's tests finish — ``_seed_company_into_cashbook_mode`` below
    mutates ``bookkeeping_mode='cashbook'``,
    ``cashbook_default_bank_account_id=<bank>``, and ``tax_registered``
    on the shared seed company. Without a teardown those mutations
    leak to every subsequent test in the session (the test stack runs
    alphabetically by directory and the seed company is shared across
    all tests), breaking ~25 invoice/payment/items/retention tests that
    expect ``bookkeeping_mode='full'``.

    Reset order matters: ``ck_cashbook_requires_bank`` (migration 0126)
    forbids ``bookkeeping_mode='cashbook'`` with a NULL bank, and the
    complement forbids non-NULL bank with ``bookkeeping_mode != 'cashbook'``.
    Single UPDATE sets both columns atomically so the CHECK sees the
    consistent end state.
    """
    yield
    # Teardown — runs once, after every test in this module has finished.
    from sqlalchemy import text

    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        if co is None:
            return  # No seed company — nothing to reset.
        # Atomic UPDATE: both columns at once so the CHECK constraint sees
        # the consistent (full, NULL bank) end state regardless of order.
        # tax_registered also gets reset to the model default (False).
        await session.execute(
            text(
                "UPDATE companies SET "
                "bookkeeping_mode = 'full', "
                "cashbook_default_bank_account_id = NULL, "
                "tax_registered = false "
                "WHERE id = :cid"
            ).bindparams(cid=co.id)
        )
        await session.commit()


async def _seed_company_into_cashbook_mode(
    *,
    tax_registered: bool = False,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Configure the test seed company into cashbook mode and return
    ``(tenant_id, company_id)``. Idempotent across tests."""
    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert co is not None, "seed company not found — check conftest seed_coa fixture"

        # Pick a bank account from the seeded AU CoA. 1-1110 = "Bank".
        bank = (
            await session.execute(
                select(Account).where(
                    Account.company_id == co.id,
                    Account.code == "1-1110",
                )
            )
        ).scalar_one_or_none()
        assert bank is not None, "AU CoA seed missing 1-1110 Bank — fixture broken"

        co.bookkeeping_mode = "cashbook"
        co.cashbook_default_bank_account_id = bank.id
        co.tax_registered = tax_registered

        # Wire up GST accounts for auto-post if registered. Settings are
        # tenant-default so we set them once; safe to overwrite.
        if tax_registered:
            await settings_svc.set(session, "gst_collected_account_code", "2-1310")
            await settings_svc.set(session, "gst_paid_account_code", "2-1330")
            await settings_svc.set(session, "gst_auto_post", "true")
        await session.commit()
        return co.tenant_id, co.id


def _new_key(prefix: str = "test") -> str:
    return f"{prefix}-{uuid.uuid4()}"


def _trial_balance(entry: JournalEntry) -> tuple[Decimal, Decimal]:
    debits = sum((ln.debit for ln in entry.lines), Decimal("0"))
    credits = sum((ln.credit for ln in entry.lines), Decimal("0"))
    return debits, credits


# ---------------------------------------------------------------------------
# Happy path — non-registered sole trader (2-line JE)
# ---------------------------------------------------------------------------


async def test_expense_non_registered_balances_two_lines() -> None:
    tenant_id, company_id = await _seed_company_into_cashbook_mode(
        tax_registered=False
    )
    async with AsyncSessionLocal() as session:
        je = await record_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_date=date(2026, 5, 8),
            description="Bunnings — drill bits",
            amount=Decimal("110.00"),
            direction="expense",
            category_code="EXP_MATERIALS",
            idempotency_key=_new_key("exp-non"),
            actor="pytest",
        )

    assert je.status == EntryStatus.POSTED
    assert len(je.lines) == 2, (
        f"Non-registered expense should be 2-line; got {len(je.lines)}"
    )
    debits, credits = _trial_balance(je)
    assert debits == credits == Decimal("110.00")
    # Cashbook meta stamped onto attachments
    assert je.attachments is not None
    assert je.attachments["cashbook_meta"]["category_code"] == "EXP_MATERIALS"
    assert je.attachments["cashbook_meta"]["direction"] == "expense"


async def test_income_non_registered_balances_two_lines() -> None:
    tenant_id, company_id = await _seed_company_into_cashbook_mode(
        tax_registered=False
    )
    async with AsyncSessionLocal() as session:
        je = await record_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_date=date(2026, 5, 8),
            description="Invoice 1043 — ABC Ltd",
            amount=Decimal("2200.00"),
            direction="income",
            category_code="INC_SERVICES",
            idempotency_key=_new_key("inc-non"),
            actor="pytest",
        )

    assert len(je.lines) == 2
    debits, credits = _trial_balance(je)
    assert debits == credits == Decimal("2200.00")


# ---------------------------------------------------------------------------
# GST-registered — auto-posted GST line, 3-line JE
# ---------------------------------------------------------------------------


async def test_expense_registered_gst_three_lines() -> None:
    tenant_id, company_id = await _seed_company_into_cashbook_mode(
        tax_registered=True
    )
    async with AsyncSessionLocal() as session:
        je = await record_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_date=date(2026, 5, 8),
            description="Bunnings — drill bits",
            amount=Decimal("110.00"),
            direction="expense",
            category_code="EXP_MATERIALS",
            idempotency_key=_new_key("exp-reg"),
            actor="pytest",
        )

    # Materials 100 + GST Paid 10 = 110; Bank 110.
    assert len(je.lines) == 3, (
        f"Registered expense with default 10% GST should be 3-line; "
        f"got {len(je.lines)}"
    )
    debits, credits = _trial_balance(je)
    assert debits == credits == Decimal("110.00")

    # Verify the lines are split correctly: a 100 net + 10 GST split.
    debit_amounts = sorted([ln.debit for ln in je.lines if ln.debit > 0])
    credit_amounts = sorted([ln.credit for ln in je.lines if ln.credit > 0])
    assert debit_amounts == [Decimal("10.00"), Decimal("100.00")]
    assert credit_amounts == [Decimal("110.00")]


async def test_income_registered_gst_three_lines() -> None:
    tenant_id, company_id = await _seed_company_into_cashbook_mode(
        tax_registered=True
    )
    async with AsyncSessionLocal() as session:
        je = await record_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_date=date(2026, 5, 8),
            description="Service invoice",
            amount=Decimal("1100.00"),
            direction="income",
            category_code="INC_SERVICES",
            idempotency_key=_new_key("inc-reg"),
            actor="pytest",
        )

    assert len(je.lines) == 3
    debits, credits = _trial_balance(je)
    assert debits == credits == Decimal("1100.00")
    # Sales credited 1000, GST Collected credited 100, Bank debited 1100.
    debit_amounts = sorted([ln.debit for ln in je.lines if ln.debit > 0])
    credit_amounts = sorted([ln.credit for ln in je.lines if ln.credit > 0])
    assert debit_amounts == [Decimal("1100.00")]
    assert credit_amounts == [Decimal("100.00"), Decimal("1000.00")]


async def test_expense_registered_gst_free_category_skips_gst_line() -> None:
    """Bank fees (EXP_BANK) are GST-free even for a GST-registered trader.
    No GST line should be generated."""
    tenant_id, company_id = await _seed_company_into_cashbook_mode(
        tax_registered=True
    )
    async with AsyncSessionLocal() as session:
        je = await record_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_date=date(2026, 5, 8),
            description="Monthly bank fee",
            amount=Decimal("15.00"),
            direction="expense",
            category_code="EXP_BANK",
            idempotency_key=_new_key("bankfee"),
            actor="pytest",
        )

    assert len(je.lines) == 2
    debits, credits = _trial_balance(je)
    assert debits == credits == Decimal("15.00")


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_idempotency_replay_returns_same_entry() -> None:
    tenant_id, company_id = await _seed_company_into_cashbook_mode(
        tax_registered=False
    )
    key = _new_key("idem")

    async with AsyncSessionLocal() as session:
        first = await record_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_date=date(2026, 5, 8),
            description="Test idempotency",
            amount=Decimal("50.00"),
            direction="expense",
            category_code="EXP_OTHER",
            idempotency_key=key,
            actor="pytest",
        )
        first_id = first.id

    async with AsyncSessionLocal() as session:
        second = await record_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_date=date(2026, 5, 8),
            description="Test idempotency",
            amount=Decimal("50.00"),
            direction="expense",
            category_code="EXP_OTHER",
            idempotency_key=key,
            actor="pytest",
        )

    assert second.id == first_id, "idempotency replay must return same JE"


async def test_different_keys_create_distinct_entries() -> None:
    tenant_id, company_id = await _seed_company_into_cashbook_mode(
        tax_registered=False
    )
    async with AsyncSessionLocal() as session:
        a = await record_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_date=date(2026, 5, 8),
            description="Entry A",
            amount=Decimal("25.00"),
            direction="expense",
            category_code="EXP_OTHER",
            idempotency_key=_new_key("a"),
            actor="pytest",
        )
        b = await record_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_date=date(2026, 5, 8),
            description="Entry B",
            amount=Decimal("25.00"),
            direction="expense",
            category_code="EXP_OTHER",
            idempotency_key=_new_key("b"),
            actor="pytest",
        )
    assert a.id != b.id


# ---------------------------------------------------------------------------
# Configuration errors
# ---------------------------------------------------------------------------


async def test_company_not_in_cashbook_mode_rejected() -> None:
    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        # Null the bank FIRST — ck_cashbook_requires_bank (migration 0126)
        # forbids non-NULL bank when bookkeeping_mode != 'cashbook'. Earlier
        # tests in this module set bank=<seeded bank> via
        # _seed_company_into_cashbook_mode, so flipping mode alone now
        # violates the check.
        co.cashbook_default_bank_account_id = None
        co.bookkeeping_mode = "full"
        await session.commit()
        tenant_id, company_id = co.tenant_id, co.id

    async with AsyncSessionLocal() as session:
        with pytest.raises(CashbookNotConfigured) as exc:
            await record_cashbook_entry(
                db=session,
                tenant_id=tenant_id,
                company_id=company_id,
                entry_date=date(2026, 5, 8),
                description="x",
                amount=Decimal("10.00"),
                direction="expense",
                category_code="EXP_OTHER",
                idempotency_key=_new_key("notmode"),
                actor="pytest",
            )
    assert exc.value.code == "cashbook_not_configured"


async def test_missing_default_bank_typed_error() -> None:
    """Drop the bank account FK directly to simulate a partially-onboarded
    company. The CHECK constraint blocks ``UPDATE companies SET
    bookkeeping_mode='cashbook'`` while the bank is NULL, so we leave
    the company in 'full' mode but force the service to read the row
    with NULL FK by flipping the mode without enforcement (test-only)."""
    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        # Set up valid cashbook mode first, then drop the bank atomically.
        # The CHECK is row-level so the constraint trips on commit.
        # We test the service's defensive guard by directly simulating
        # the misconfigured row via raw SQL that bypasses the CHECK.
        from sqlalchemy import text
        await session.execute(
            text(
                "UPDATE companies "
                "SET bookkeeping_mode = 'full', "
                "    cashbook_default_bank_account_id = NULL "
                "WHERE id = :cid"
            ).bindparams(cid=co.id)
        )
        await session.commit()
        tenant_id, company_id = co.tenant_id, co.id

    # Force-flip mode in raw SQL to bypass CHECK (test-only path).
    async with AsyncSessionLocal() as session:
        from sqlalchemy import text
        # Drop the constraint, flip the mode, restore the constraint.
        # This is a deliberate test escape hatch to verify the defensive
        # guard inside the service. Real customers can't reach this state.
        await session.execute(
            text("ALTER TABLE companies DROP CONSTRAINT ck_cashbook_requires_bank")
        )
        await session.execute(
            text(
                "UPDATE companies SET bookkeeping_mode = 'cashbook' "
                "WHERE id = :cid"
            ).bindparams(cid=company_id)
        )
        await session.commit()

    try:
        async with AsyncSessionLocal() as session:
            with pytest.raises(CashbookNotConfigured) as exc:
                await record_cashbook_entry(
                    db=session,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    entry_date=date(2026, 5, 8),
                    description="x",
                    amount=Decimal("10.00"),
                    direction="expense",
                    category_code="EXP_OTHER",
                    idempotency_key=_new_key("nobank"),
                    actor="pytest",
                )
        assert exc.value.code == "cashbook_not_configured"
        assert "bank account" in str(exc.value).lower()
    finally:
        # Always restore the constraint, then put the company back in
        # 'full' so subsequent tests aren't poisoned.
        async with AsyncSessionLocal() as session:
            from sqlalchemy import text
            await session.execute(
                text(
                    "UPDATE companies SET bookkeeping_mode = 'full' "
                    "WHERE id = :cid"
                ).bindparams(cid=company_id)
            )
            await session.execute(
                text(
                    "ALTER TABLE companies ADD CONSTRAINT "
                    "ck_cashbook_requires_bank CHECK ("
                    "bookkeeping_mode <> 'cashbook' "
                    "OR cashbook_default_bank_account_id IS NOT NULL"
                    ")"
                )
            )
            await session.commit()


async def test_non_aud_base_currency_refused() -> None:
    tenant_id, company_id = await _seed_company_into_cashbook_mode(
        tax_registered=False
    )
    async with AsyncSessionLocal() as session:
        from sqlalchemy import text
        await session.execute(
            text(
                "UPDATE companies SET base_currency = 'USD' "
                "WHERE id = :cid"
            ).bindparams(cid=company_id)
        )
        await session.commit()

    try:
        async with AsyncSessionLocal() as session:
            with pytest.raises(CashbookCurrencyError) as exc:
                await record_cashbook_entry(
                    db=session,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    entry_date=date(2026, 5, 8),
                    description="x",
                    amount=Decimal("10.00"),
                    direction="expense",
                    category_code="EXP_OTHER",
                    idempotency_key=_new_key("usd"),
                    actor="pytest",
                )
        assert exc.value.code == "cashbook_currency_unsupported"
    finally:
        async with AsyncSessionLocal() as session:
            from sqlalchemy import text
            await session.execute(
                text(
                    "UPDATE companies SET base_currency = 'AUD' "
                    "WHERE id = :cid"
                ).bindparams(cid=company_id)
            )
            await session.commit()


# ---------------------------------------------------------------------------
# Category errors
# ---------------------------------------------------------------------------


async def test_unknown_category_rejected() -> None:
    tenant_id, company_id = await _seed_company_into_cashbook_mode(
        tax_registered=False
    )
    async with AsyncSessionLocal() as session:
        with pytest.raises(CashbookCategoryError):
            await record_cashbook_entry(
                db=session,
                tenant_id=tenant_id,
                company_id=company_id,
                entry_date=date(2026, 5, 8),
                description="x",
                amount=Decimal("10.00"),
                direction="expense",
                category_code="EXP_NOT_REAL",
                idempotency_key=_new_key("unk"),
                actor="pytest",
            )


async def test_wrong_direction_rejected() -> None:
    """Logging an income category as an expense should be rejected."""
    tenant_id, company_id = await _seed_company_into_cashbook_mode(
        tax_registered=False
    )
    async with AsyncSessionLocal() as session:
        with pytest.raises(CashbookCategoryError) as exc:
            await record_cashbook_entry(
                db=session,
                tenant_id=tenant_id,
                company_id=company_id,
                entry_date=date(2026, 5, 8),
                description="x",
                amount=Decimal("10.00"),
                direction="expense",
                category_code="INC_SALES",
                idempotency_key=_new_key("dir"),
                actor="pytest",
            )
    assert exc.value.code == "cashbook_category_invalid"


async def test_transfer_category_rejected_in_income_expense_flow() -> None:
    tenant_id, company_id = await _seed_company_into_cashbook_mode(
        tax_registered=False
    )
    async with AsyncSessionLocal() as session:
        with pytest.raises(CashbookCategoryError) as exc:
            await record_cashbook_entry(
                db=session,
                tenant_id=tenant_id,
                company_id=company_id,
                entry_date=date(2026, 5, 8),
                description="Transfer",
                amount=Decimal("100.00"),
                direction="expense",
                category_code="TX_TRANSFER",
                idempotency_key=_new_key("txn"),
                actor="pytest",
            )
    assert "transfer" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Amount validation
# ---------------------------------------------------------------------------


async def test_zero_amount_rejected() -> None:
    tenant_id, company_id = await _seed_company_into_cashbook_mode(
        tax_registered=False
    )
    async with AsyncSessionLocal() as session:
        with pytest.raises(CashbookError, match="positive"):
            await record_cashbook_entry(
                db=session,
                tenant_id=tenant_id,
                company_id=company_id,
                entry_date=date(2026, 5, 8),
                description="x",
                amount=Decimal("0"),
                direction="expense",
                category_code="EXP_OTHER",
                idempotency_key=_new_key("zero"),
                actor="pytest",
            )


async def test_negative_amount_rejected() -> None:
    tenant_id, company_id = await _seed_company_into_cashbook_mode(
        tax_registered=False
    )
    async with AsyncSessionLocal() as session:
        with pytest.raises(CashbookError, match="positive"):
            await record_cashbook_entry(
                db=session,
                tenant_id=tenant_id,
                company_id=company_id,
                entry_date=date(2026, 5, 8),
                description="x",
                amount=Decimal("-50.00"),
                direction="expense",
                category_code="EXP_OTHER",
                idempotency_key=_new_key("neg"),
                actor="pytest",
            )


# ---------------------------------------------------------------------------
# Phase B.5 — void / replace
# ---------------------------------------------------------------------------


async def test_void_flips_status_and_posts_reversal() -> None:
    """``void_cashbook_entry`` reverses a posted JE and flips it to REVERSED."""
    from saebooks.services.cashbook import void_cashbook_entry

    tenant_id, company_id = await _seed_company_into_cashbook_mode(
        tax_registered=False
    )
    async with AsyncSessionLocal() as session:
        original = await record_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_date=date(2026, 5, 8),
            description="to be voided",
            amount=Decimal("75.00"),
            direction="expense",
            category_code="EXP_OTHER",
            idempotency_key=_new_key("void-orig"),
            actor="pytest",
        )
        original_id = original.id

        reversal = await void_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_id=original_id,
            actor="pytest",
        )

    # Re-fetch in fresh session to confirm persisted state.
    async with AsyncSessionLocal() as session:
        refreshed_orig = (
            await session.execute(
                select(JournalEntry).where(JournalEntry.id == original_id)
            )
        ).scalar_one()
        assert refreshed_orig.status == EntryStatus.REVERSED
        assert reversal.reversal_of_id == original_id
        assert reversal.status == EntryStatus.POSTED
        # Reversal JE has no cashbook_meta — keeps it out of cashbook list/get.
        assert not (reversal.attachments or {}).get("cashbook_meta")


async def test_void_already_reversed_returns_existing_reversal() -> None:
    """Void is idempotent — re-voiding returns the same reversal JE."""
    from saebooks.services.cashbook import void_cashbook_entry

    tenant_id, company_id = await _seed_company_into_cashbook_mode(
        tax_registered=False
    )
    async with AsyncSessionLocal() as session:
        original = await record_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_date=date(2026, 5, 8),
            description="double-void",
            amount=Decimal("10.00"),
            direction="expense",
            category_code="EXP_OTHER",
            idempotency_key=_new_key("dbl-void"),
            actor="pytest",
        )
        rev1 = await void_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_id=original.id,
            actor="pytest",
        )
        rev2 = await void_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_id=original.id,
            actor="pytest",
        )
    assert rev1.id == rev2.id


async def test_void_unknown_entry_raises_not_found() -> None:
    from saebooks.services.cashbook import (
        CashbookEntryNotFound,
        void_cashbook_entry,
    )

    tenant_id, company_id = await _seed_company_into_cashbook_mode(
        tax_registered=False
    )
    async with AsyncSessionLocal() as session:
        with pytest.raises(CashbookEntryNotFound):
            await void_cashbook_entry(
                db=session,
                tenant_id=tenant_id,
                company_id=company_id,
                entry_id=uuid.uuid4(),
                actor="pytest",
            )


async def test_replace_voids_original_and_creates_replacement() -> None:
    """``replace_cashbook_entry`` voids the original and creates a new
    JE with the new payload + ``replaces_id`` link."""
    from saebooks.services.cashbook import replace_cashbook_entry

    tenant_id, company_id = await _seed_company_into_cashbook_mode(
        tax_registered=False
    )
    async with AsyncSessionLocal() as session:
        original = await record_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_date=date(2026, 5, 8),
            description="original payload",
            amount=Decimal("100.00"),
            direction="expense",
            category_code="EXP_OTHER",
            idempotency_key=_new_key("orig-pl"),
            actor="pytest",
        )
        original_id = original.id

        new_je = await replace_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_id=original_id,
            entry_date=date(2026, 5, 8),
            description="new payload",
            amount=Decimal("250.00"),
            direction="expense",
            category_code="EXP_TOOLS",
            idempotency_key=_new_key("new-pl"),
            actor="pytest",
        )

    async with AsyncSessionLocal() as session:
        refreshed_orig = (
            await session.execute(
                select(JournalEntry).where(JournalEntry.id == original_id)
            )
        ).scalar_one()
        assert refreshed_orig.status == EntryStatus.REVERSED

        refreshed_new = (
            await session.execute(
                select(JournalEntry).where(JournalEntry.id == new_je.id)
            )
        ).scalar_one()
        assert refreshed_new.status == EntryStatus.POSTED
        meta = (refreshed_new.attachments or {}).get("cashbook_meta") or {}
        assert meta.get("replaces_id") == str(original_id)
        assert meta.get("category_code") == "EXP_TOOLS"
        assert meta.get("gross_amount") == "250.00"


async def test_replace_idempotency_replay_returns_same_replacement() -> None:
    from saebooks.services.cashbook import replace_cashbook_entry

    tenant_id, company_id = await _seed_company_into_cashbook_mode(
        tax_registered=False
    )
    async with AsyncSessionLocal() as session:
        original = await record_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_date=date(2026, 5, 8),
            description="original",
            amount=Decimal("60.00"),
            direction="expense",
            category_code="EXP_OTHER",
            idempotency_key=_new_key("rep-orig"),
            actor="pytest",
        )
        replace_key = _new_key("rep-new")
        first = await replace_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_id=original.id,
            entry_date=date(2026, 5, 8),
            description="new",
            amount=Decimal("65.00"),
            direction="expense",
            category_code="EXP_OTHER",
            idempotency_key=replace_key,
            actor="pytest",
        )
        second = await replace_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_id=original.id,
            entry_date=date(2026, 5, 8),
            description="new",
            amount=Decimal("65.00"),
            direction="expense",
            category_code="EXP_OTHER",
            idempotency_key=replace_key,
            actor="pytest",
        )
    assert first.id == second.id
