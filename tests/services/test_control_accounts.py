"""Tests for ``saebooks.services.control_accounts`` (Packet 4b).

Covers:
* ``resolve_ar_code`` / ``resolve_ap_code`` — NULL override falls back
  to the AU convention codes; a set override wins.
* ``get_ar_account`` / ``get_ap_account`` — resolve the Account row for
  the resolved code; raise the caller-supplied ``error_cls`` (loud, not
  silent) when it doesn't exist in the chart.
* End-to-end: ``invoices.post_invoice`` / ``bills.post_bill`` actually
  post against a company's OVERRIDDEN control-account code, not the
  hardcoded AU one — proves the wiring, not just the resolver in
  isolation. These companies deliberately do NOT have a "1-1200" /
  "2-1200" account at all, so if any call site still hardcoded the AU
  code the posting would fail loudly (account not found) rather than
  silently misposting.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine
from saebooks.services import bills as bills_svc
from saebooks.services import control_accounts as svc
from saebooks.services import invoices as invoices_svc

pytestmark = pytest.mark.postgres_only


class _DummyError(ValueError):
    pass


async def _override_company(
    *, ar_code: str | None, ap_code: str | None
) -> uuid.UUID:
    """A throwaway company with the given AR/AP control-account override
    (or NULL) and NO "1-1200"/"2-1200" accounts at all."""
    cid = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Company(
                id=cid,
                name=f"Control Account Test {cid.hex[:8]}",
                base_currency="EUR",
                fin_year_start_month=1,
                audit_mode="immutable",
                ar_control_account_code=ar_code,
                ap_control_account_code=ap_code,
            )
        )
        await session.commit()
    return cid


# ---------------------------------------------------------------------------
# resolve_ar_code / resolve_ap_code
# ---------------------------------------------------------------------------


async def test_resolve_codes_default_to_au_when_unset() -> None:
    cid = await _override_company(ar_code=None, ap_code=None)
    async with AsyncSessionLocal() as session:
        assert await svc.resolve_ar_code(session, cid) == "1-1200"
        assert await svc.resolve_ap_code(session, cid) == "2-1200"


async def test_resolve_codes_use_override_when_set() -> None:
    cid = await _override_company(ar_code="1000", ap_code="2000")
    async with AsyncSessionLocal() as session:
        assert await svc.resolve_ar_code(session, cid) == "1000"
        assert await svc.resolve_ap_code(session, cid) == "2000"


async def test_resolve_codes_blank_string_treated_as_unset() -> None:
    cid = await _override_company(ar_code="  ", ap_code="")
    async with AsyncSessionLocal() as session:
        assert await svc.resolve_ar_code(session, cid) == "1-1200"
        assert await svc.resolve_ap_code(session, cid) == "2-1200"


async def _override_company_ee(
    *, ar_code: str | None, ap_code: str | None
) -> uuid.UUID:
    """Same as ``_override_company`` but jurisdiction="EE" -- exercises the
    per-jurisdiction fallback."""
    cid = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Company(
                id=cid,
                name=f"EE Control Account Test {cid.hex[:8]}",
                base_currency="EUR",
                fin_year_start_month=1,
                audit_mode="immutable",
                jurisdiction="EE",
                coa_template_key="ee/default",
                ar_control_account_code=ar_code,
                ap_control_account_code=ap_code,
            )
        )
        await session.commit()
    return cid


async def test_resolve_codes_default_to_ee_convention_when_unset() -> None:
    """An EE company's NULL override must fall back to the EE chart's own
    control-account codes ("1200"/"2100"), not the AU convention
    ("1-1200"/"2-1200") -- those AU codes don't exist in an EE chart, so
    falling back to them breaks posting for a company whose override was
    cleared (e.g. via PATCH)."""
    cid = await _override_company_ee(ar_code=None, ap_code=None)
    async with AsyncSessionLocal() as session:
        assert await svc.resolve_ar_code(session, cid) == "1200"
        assert await svc.resolve_ap_code(session, cid) == "2100"


async def test_resolve_codes_ee_override_still_wins() -> None:
    cid = await _override_company_ee(ar_code="1500", ap_code="2500")
    async with AsyncSessionLocal() as session:
        assert await svc.resolve_ar_code(session, cid) == "1500"
        assert await svc.resolve_ap_code(session, cid) == "2500"


# ---------------------------------------------------------------------------
# get_ar_account / get_ap_account
# ---------------------------------------------------------------------------


async def test_get_ar_account_resolves_override_code() -> None:
    cid = await _override_company(ar_code="1000", ap_code=None)
    async with AsyncSessionLocal() as session:
        acct = Account(
            company_id=cid, code="1000", name="Debtors", account_type=AccountType.ASSET
        )
        session.add(acct)
        await session.commit()
        await session.refresh(acct)

    async with AsyncSessionLocal() as session:
        resolved = await svc.get_ar_account(session, cid)
        assert resolved.id == acct.id
        assert resolved.code == "1000"


async def test_get_ar_account_missing_raises_caller_error_cls() -> None:
    cid = await _override_company(ar_code="1000", ap_code=None)
    async with AsyncSessionLocal() as session:
        with pytest.raises(_DummyError, match="1000"):
            await svc.get_ar_account(session, cid, error_cls=_DummyError)


async def test_get_ap_account_missing_raises_value_error_by_default() -> None:
    cid = await _override_company(ar_code=None, ap_code="2000")
    async with AsyncSessionLocal() as session:
        with pytest.raises(ValueError, match="2000"):
            await svc.get_ap_account(session, cid)


# ---------------------------------------------------------------------------
# End-to-end — invoices.post_invoice / bills.post_bill honour the override
# ---------------------------------------------------------------------------


async def _posted_journal_lines(entry_id: uuid.UUID) -> list[JournalLine]:
    async with AsyncSessionLocal() as session:
        entry = (
            await session.execute(
                select(JournalEntry).where(JournalEntry.id == entry_id)
            )
        ).scalar_one()
        assert entry.status == EntryStatus.POSTED
        lines = (
            await session.execute(
                select(JournalLine).where(JournalLine.entry_id == entry_id)
            )
        ).scalars().all()
        return list(lines)


async def test_post_invoice_uses_overridden_ar_code() -> None:
    cid = await _override_company(ar_code="1000", ap_code=None)
    async with AsyncSessionLocal() as session:
        session.add_all(
            [
                Account(
                    company_id=cid, code="1000", name="Debtors",
                    account_type=AccountType.ASSET,
                ),
                Account(
                    company_id=cid, code="4-6000", name="Sales",
                    account_type=AccountType.INCOME,
                ),
            ]
        )
        contact = Contact(
            company_id=cid, name="EU Customer OU", contact_type=ContactType.CUSTOMER,
        )
        session.add(contact)
        await session.commit()
        await session.refresh(contact)
        income_id = (
            await session.execute(
                select(Account.id).where(
                    Account.company_id == cid, Account.code == "4-6000"
                )
            )
        ).scalar_one()
        ar_id = (
            await session.execute(
                select(Account.id).where(
                    Account.company_id == cid, Account.code == "1000"
                )
            )
        ).scalar_one()

    async with AsyncSessionLocal() as session:
        inv = await invoices_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact.id,
            issue_date=date(2026, 7, 1),
            due_date=date(2026, 7, 31),
            lines=[
                {
                    "description": "Consulting",
                    "account_id": income_id,
                    "tax_code_id": None,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("100"),
                    "discount_pct": Decimal("0"),
                },
            ],
        )

    async with AsyncSessionLocal() as session:
        posted = await invoices_svc.post_invoice(session, inv.id)
        assert posted.journal_entry_id is not None
        entry_id = posted.journal_entry_id

    lines = await _posted_journal_lines(entry_id)
    ar_lines = [ln for ln in lines if ln.account_id == ar_id]
    assert len(ar_lines) == 1
    assert ar_lines[0].debit == Decimal("100.00")


async def test_post_bill_uses_overridden_ap_code() -> None:
    cid = await _override_company(ar_code=None, ap_code="2000")
    async with AsyncSessionLocal() as session:
        session.add_all(
            [
                Account(
                    company_id=cid, code="2000", name="Creditors",
                    account_type=AccountType.LIABILITY,
                ),
                Account(
                    company_id=cid, code="6-1000", name="Purchases",
                    account_type=AccountType.EXPENSE,
                ),
            ]
        )
        contact = Contact(
            company_id=cid, name="EU Supplier OU", contact_type=ContactType.SUPPLIER,
        )
        session.add(contact)
        await session.commit()
        await session.refresh(contact)
        expense_id = (
            await session.execute(
                select(Account.id).where(
                    Account.company_id == cid, Account.code == "6-1000"
                )
            )
        ).scalar_one()
        ap_id = (
            await session.execute(
                select(Account.id).where(
                    Account.company_id == cid, Account.code == "2000"
                )
            )
        ).scalar_one()

    async with AsyncSessionLocal() as session:
        bill = await bills_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact.id,
            issue_date=date(2026, 7, 1),
            due_date=date(2026, 7, 31),
            lines=[
                {
                    "description": "Purchases",
                    "account_id": expense_id,
                    "tax_code_id": None,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("50"),
                    "discount_pct": Decimal("0"),
                },
            ],
        )

    async with AsyncSessionLocal() as session:
        posted = await bills_svc.post_bill(session, bill.id)
        assert posted.journal_entry_id is not None
        entry_id = posted.journal_entry_id

    lines = await _posted_journal_lines(entry_id)
    ap_lines = [ln for ln in lines if ln.account_id == ap_id]
    assert len(ap_lines) == 1
    assert ap_lines[0].credit == Decimal("50.00")
