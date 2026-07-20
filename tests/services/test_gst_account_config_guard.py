"""Regression tests for the GST account-config silent-failure hardening.

Root cause (primary, 2026-06-10): ``gst_paid_account_code`` was set to
``"2-1330"``, which did not exist in that tenant's chart. ``_get_gst_account``
returned ``None`` and ``auto_post_gst_lines`` hit ``if not paid_acct: return []``
— so a taxable expense's Dr GST-Paid line was silently dropped, the journal
entry came out unbalanced, and ``/post`` failed with a misleading
"JE unbalanced" error instead of pointing at the bad setting.

These tests pin the fix: a taxable line whose GST account code does not
resolve now raises ``TaxConfigError`` (a ``PostingError`` subclass) with a
clear message, and the configuration-time helper
``validate_gst_account_settings`` flags the same condition on settings save.

Mirrors the cashbook fixture pattern in tests/services/test_cashbook.py — the
``record_cashbook_entry`` path drives ``auto_post_gst_lines`` through the real
post + balance-check pipeline.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from saebooks.db import AsyncSessionLocal
from saebooks.jurisdictions.au.tax import (
    TaxConfigError,
    validate_gst_account_settings,
)
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.journal import EntryStatus
from saebooks.services import settings as settings_svc
from saebooks.services.cashbook import record_cashbook_entry
from saebooks.services.journal import PostingError

pytestmark = pytest.mark.postgres_only


# ---------------------------------------------------------------------------
# Fixture — reuse the shared seed company in cashbook mode, GST-registered.
# Teardown restores full mode + good GST settings so nothing leaks.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
async def _restore_seed_company_after_module():
    yield
    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        if co is None:
            return
        await session.execute(
            text(
                "UPDATE companies SET "
                "bookkeeping_mode = 'full', "
                "cashbook_default_bank_account_id = NULL, "
                "tax_registered = false "
                "WHERE id = :cid"
            ).bindparams(cid=co.id)
        )
        # Restore the canonical good GST account codes so later test
        # modules that rely on them are unaffected.
        await settings_svc.set(session, "gst_collected_account_code", "2-1310")
        await settings_svc.set(session, "gst_paid_account_code", "2-1330")
        await session.commit()


async def _seed_cashbook_tax_registered(
    *,
    gst_paid_code: str = "2-1330",
    gst_collected_code: str = "2-1310",
) -> tuple[uuid.UUID, uuid.UUID]:
    """Put the seed company in GST-registered cashbook mode with the given
    GST account codes. Returns ``(tenant_id, company_id)``."""
    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert co is not None, "seed company not found"
        bank = (
            await session.execute(
                select(Account).where(
                    Account.company_id == co.id, Account.code == "1-1110"
                )
            )
        ).scalar_one_or_none()
        assert bank is not None, "AU CoA seed missing 1-1110 Bank"
        co.bookkeeping_mode = "cashbook"
        co.cashbook_default_bank_account_id = bank.id
        co.tax_registered = True
        await settings_svc.set(session, "gst_collected_account_code", gst_collected_code)
        await settings_svc.set(session, "gst_paid_account_code", gst_paid_code)
        await settings_svc.set(session, "gst_auto_post", "true")
        await session.commit()
        return co.tenant_id, co.id


def _key(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


# ---------------------------------------------------------------------------
# Test 1 (regression) — correctly-configured GST account → balanced 3-line JE
# with the Dr GST-Paid line present.
# ---------------------------------------------------------------------------


async def test_taxable_expense_with_good_gst_account_balances() -> None:
    tenant_id, company_id = await _seed_cashbook_tax_registered(
        gst_paid_code="2-1330"
    )
    async with AsyncSessionLocal() as session:
        je = await record_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_date=date(2026, 5, 8),
            description="Bunnings — consumables",
            amount=Decimal("110.00"),
            direction="expense",
            category_code="EXP_MATERIALS",
            idempotency_key=_key("good-gst"),
            actor="pytest",
        )
        assert je.status == EntryStatus.POSTED
        assert len(je.lines) == 3, "expected materials + GST-Paid + bank"
        debits = sum((ln.debit for ln in je.lines), Decimal("0"))
        credits = sum((ln.credit for ln in je.lines), Decimal("0"))
        assert debits == credits == Decimal("110.00")

        # The Dr GST-Paid line lands on the resolved 2-1330 account.
        gst_paid_acct = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company_id, Account.code == "2-1330"
                )
            )
        ).scalar_one()
        gst_lines = [
            ln for ln in je.lines if ln.account_id == gst_paid_acct.id
        ]
        assert len(gst_lines) == 1
        assert gst_lines[0].debit == Decimal("10.00")


# ---------------------------------------------------------------------------
# Test 2 (the actual bug) — unresolvable gst_paid_account_code on a taxable
# expense raises TaxConfigError, NOT a silent unbalanced JE.
# ---------------------------------------------------------------------------


async def test_taxable_expense_with_bad_gst_account_raises_config_error() -> None:
    # "9-9999" does not exist in the seeded AU chart — same failure mode as
    # the production "2-1330" on the primary tenant.
    tenant_id, company_id = await _seed_cashbook_tax_registered(
        gst_paid_code="9-9999"
    )
    async with AsyncSessionLocal() as session:
        with pytest.raises(TaxConfigError) as excinfo:
            await record_cashbook_entry(
                db=session,
                tenant_id=tenant_id,
                company_id=company_id,
                entry_date=date(2026, 5, 8),
                description="Bunnings — consumables",
                amount=Decimal("110.00"),
                direction="expense",
                category_code="EXP_MATERIALS",
                idempotency_key=_key("bad-gst"),
                actor="pytest",
            )
    msg = str(excinfo.value)
    assert "gst_paid_account_code" in msg
    assert "9-9999" in msg
    assert "does not resolve" in msg
    # It is a PostingError subclass, so every existing
    # ``except journal_svc.PostingError`` handler surfaces it cleanly.
    assert isinstance(excinfo.value, PostingError)


# ---------------------------------------------------------------------------
# Test 3 (unaffected) — a GST-free line posts fine with no GST line even when
# the GST account code is unresolvable: no taxable line means no config error.
# ---------------------------------------------------------------------------


async def test_gst_free_expense_unaffected_by_bad_gst_account() -> None:
    # Bad GST-paid code, but EXP_BANK is GST-free → no taxable line → no GST
    # account needed → must post cleanly as a 2-line JE.
    tenant_id, company_id = await _seed_cashbook_tax_registered(
        gst_paid_code="9-9999"
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
            idempotency_key=_key("gst-free"),
            actor="pytest",
        )
        assert je.status == EntryStatus.POSTED
        assert len(je.lines) == 2
        debits = sum((ln.debit for ln in je.lines), Decimal("0"))
        credits = sum((ln.credit for ln in je.lines), Decimal("0"))
        assert debits == credits == Decimal("15.00")


# ---------------------------------------------------------------------------
# Settings-validation helper — catches the bad code at configuration time.
# ---------------------------------------------------------------------------


async def test_validate_gst_account_settings_flags_unresolvable_code() -> None:
    _tenant_id, company_id = await _seed_cashbook_tax_registered(
        gst_paid_code="9-9999", gst_collected_code="2-1310"
    )
    async with AsyncSessionLocal() as session:
        problems = await validate_gst_account_settings(session, company_id)
    assert "gst_paid_account_code" in problems
    assert "9-9999" in problems["gst_paid_account_code"]
    # The good collected code is not flagged.
    assert "gst_collected_account_code" not in problems


async def test_validate_gst_account_settings_clean_when_all_resolve() -> None:
    _tenant_id, company_id = await _seed_cashbook_tax_registered(
        gst_paid_code="2-1330", gst_collected_code="2-1310"
    )
    async with AsyncSessionLocal() as session:
        # Clearing account is unset by default — tolerated unless require_set.
        problems = await validate_gst_account_settings(
            session,
            company_id,
            keys=("gst_collected_account_code", "gst_paid_account_code"),
        )
    assert problems == {}
