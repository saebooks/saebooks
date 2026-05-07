"""Tests for ``saebooks.services.pay_run``.

Integration-level: hits Postgres through `AsyncSessionLocal` to set
up a bank account + supplier + bill, then drives
``build_aba_from_selection`` end-to-end and asserts the returned
string parses back to the expected CEMTEX.

Splits into two halves:

* candidate listing (``candidates_for_payrun``) — ensures only
  POSTED, non-archived, balance-owing bills show up.
* file building — remitter/payee validation, amount caps,
  end-to-end byte shape.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.bill import Bill
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.document_counter import DocumentCounter
from saebooks.services import bills as bill_svc
from saebooks.services import pay_run as svc


async def _fast_forward_bill_counter() -> None:
    """Advance the per-company bill DocumentCounter past any existing
    BILL-NNNNNN number — see ``test_bills.py`` for the full rationale.
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
                select(Bill.number).where(
                    Bill.company_id == company.id,
                    Bill.number.isnot(None),
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
                    DocumentCounter.kind == "bill",
                )
            )
        ).scalar_one_or_none()
        if counter is None:
            counter = DocumentCounter(
                company_id=company.id,
                kind="bill",
                prefix="BILL-",
                pad_width=6,
                next_value=max_suffix + 1,
            )
            session.add(counter)
        elif counter.next_value <= max_suffix:
            counter.next_value = max_suffix + 1
        await session.commit()


@pytest.fixture(autouse=True, scope="module")
async def _prep_bill_counter() -> AsyncGenerator[None, None]:
    await _fast_forward_bill_counter()
    yield

_REMITTER_CODE = "1-1180"  # synthesised bank account for ABA tests
_REMITTER_NAME = "ABA Test Bank Account"


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company_id, bank_account_id, expense_account_id, supplier_id)."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None

        # Dedicated bank account so other tests that read 1-1110 don't
        # conflict with ABA credentials. Idempotent.
        bank = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.code == _REMITTER_CODE,
                )
            )
        ).scalars().first()
        if bank is None:
            bank = Account(
                company_id=company.id,
                code=_REMITTER_CODE,
                name=_REMITTER_NAME,
                account_type=AccountType.ASSET,
                reconcile=True,
            )
            session.add(bank)
            await session.commit()
            await session.refresh(bank)

        # Populate ABA fields on the bank if blank (idempotent).
        bank.bsb = "062-000"
        bank.bank_account_number = "11112222"
        bank.bank_account_title = "SAE ENGINEERING"
        bank.apca_user_id = "301500"
        bank.bank_abbreviation = "CBA"
        await session.commit()

        expense = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.code == "6-1000",
                )
            )
        ).scalar_one()

        supplier = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == company.id,
                    Contact.name == "ABA Test Supplier",
                )
            )
        ).scalars().first()
        if supplier is None:
            supplier = Contact(
                company_id=company.id,
                name="ABA Test Supplier",
                contact_type=ContactType.SUPPLIER,
            )
            session.add(supplier)
            await session.commit()
            await session.refresh(supplier)
        # Populate payee ABA fields idempotently.
        supplier.bank_bsb = "062-001"
        supplier.bank_account_number = "87654321"
        supplier.bank_account_title = "ABA TEST SUPPLIER"
        await session.commit()

        return company.id, bank.id, expense.id, supplier.id


async def _post_bill(
    company_id: uuid.UUID,
    supplier_id: uuid.UUID,
    expense_account_id: uuid.UUID,
    *,
    total: Decimal,
    due_offset_days: int = 7,
) -> uuid.UUID:
    today = date(2026, 4, 20)
    async with AsyncSessionLocal() as session:
        bill = await bill_svc.create_draft(
            session,
            company_id=company_id,
            contact_id=supplier_id,
            issue_date=today,
            due_date=today + timedelta(days=due_offset_days),
            lines=[
                {
                    "description": "ABA test line",
                    "account_id": expense_account_id,
                    "tax_code_id": None,
                    "quantity": Decimal("1"),
                    "unit_price": total,
                    "discount_pct": Decimal("0"),
                }
            ],
        )
        posted = await bill_svc.post_bill(session, bill.id, posted_by="tests")
    return posted.id


# ---------------------------------------------------------------------- #
# candidates_for_payrun                                                   #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_candidates_returns_posted_with_balance() -> None:
    cid, _bank, expense, supplier = await _ctx()
    bill_id = await _post_bill(
        cid, supplier, expense, total=Decimal("123.45")
    )
    async with AsyncSessionLocal() as session:
        rows = await svc.candidates_for_payrun(session, cid)
    assert any(c.bill.id == bill_id for c in rows)
    picked = next(c for c in rows if c.bill.id == bill_id)
    assert picked.balance_due == Decimal("123.45")
    assert picked.contact.id == supplier


@pytest.mark.asyncio
async def test_candidates_excludes_fully_paid() -> None:
    cid, _bank, expense, supplier = await _ctx()
    bill_id = await _post_bill(
        cid, supplier, expense, total=Decimal("50.00")
    )
    async with AsyncSessionLocal() as session:
        bill = await bill_svc.get(session, bill_id)
        bill.amount_paid = Decimal("50.00")  # fully paid
        await session.commit()
    async with AsyncSessionLocal() as session:
        rows = await svc.candidates_for_payrun(session, cid)
    assert not any(c.bill.id == bill_id for c in rows)


# ---------------------------------------------------------------------- #
# build_aba_from_selection                                                 #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_build_aba_happy_path() -> None:
    cid, bank, expense, supplier = await _ctx()
    bill_id = await _post_bill(
        cid, supplier, expense, total=Decimal("250.00")
    )

    async with AsyncSessionLocal() as session:
        text = await svc.build_aba_from_selection(
            session,
            cid,
            bank_account_id=bank,
            selections=[
                svc.PayRunSelection(
                    bill_id=bill_id, amount=Decimal("250.00")
                )
            ],
            process_date=date(2026, 4, 21),
        )

    lines = text.rstrip("\r\n").split("\r\n")
    assert len(lines) == 3  # header + 1 detail + trailer
    assert all(len(ln) == 120 for ln in lines)

    header, detail, trailer = lines
    assert header[20:23] == "CBA"
    assert header[56:62] == "301500"
    assert header[74:80] == "210426"

    assert detail[1:8] == "062-001"               # payee BSB
    assert detail[8:17] == "87654321".rjust(9)    # payee account
    assert detail[20:30] == "0000025000"          # $250 = 25000c

    assert trailer[20:30] == "0000025000"          # net
    assert trailer[30:40] == "0000025000"          # credit total
    assert trailer[74:80] == "000001"              # one item


@pytest.mark.asyncio
async def test_build_rejects_over_balance() -> None:
    cid, bank, expense, supplier = await _ctx()
    bill_id = await _post_bill(
        cid, supplier, expense, total=Decimal("100.00")
    )
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.PayRunError, match="balance due"):
            await svc.build_aba_from_selection(
                session,
                cid,
                bank_account_id=bank,
                selections=[
                    svc.PayRunSelection(
                        bill_id=bill_id, amount=Decimal("150.00")
                    )
                ],
                process_date=date(2026, 4, 21),
            )


@pytest.mark.asyncio
async def test_build_rejects_empty_selections() -> None:
    cid, bank, _expense, _supplier = await _ctx()
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.PayRunError, match="at least one"):
            await svc.build_aba_from_selection(
                session,
                cid,
                bank_account_id=bank,
                selections=[],
                process_date=date(2026, 4, 21),
            )


@pytest.mark.asyncio
async def test_build_rejects_bank_account_missing_aba_fields() -> None:
    cid, _bank, expense, supplier = await _ctx()
    bill_id = await _post_bill(
        cid, supplier, expense, total=Decimal("10.00")
    )
    # Make a second bank account with no ABA fields set (idempotent —
    # reused across test runs against the persistent dev DB).
    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(
                select(Account).where(
                    Account.company_id == cid,
                    Account.code == "1-1181",
                )
            )
        ).scalars().first()
        if existing is None:
            blank_bank = Account(
                company_id=cid,
                code="1-1181",
                name="Blank Bank",
                account_type=AccountType.ASSET,
            )
            session.add(blank_bank)
            await session.commit()
            await session.refresh(blank_bank)
            blank_id = blank_bank.id
        else:
            # Guarantee no ABA fields are present on reused row.
            existing.bsb = None
            existing.bank_account_number = None
            existing.bank_account_title = None
            existing.apca_user_id = None
            existing.bank_abbreviation = None
            await session.commit()
            blank_id = existing.id

    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.PayRunError, match="missing ABA fields"):
            await svc.build_aba_from_selection(
                session,
                cid,
                bank_account_id=blank_id,
                selections=[
                    svc.PayRunSelection(
                        bill_id=bill_id, amount=Decimal("10.00")
                    )
                ],
                process_date=date(2026, 4, 21),
            )


@pytest.mark.asyncio
async def test_build_rejects_supplier_missing_aba_fields() -> None:
    cid, bank, expense, _supplier = await _ctx()
    # Supplier with no bank fields (idempotent across runs).
    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == cid,
                    Contact.name == "Nobody's Bank Ltd",
                )
            )
        ).scalars().first()
        if existing is None:
            nobank = Contact(
                company_id=cid,
                name="Nobody's Bank Ltd",
                contact_type=ContactType.SUPPLIER,
            )
            session.add(nobank)
            await session.commit()
            await session.refresh(nobank)
            supplier_id = nobank.id
        else:
            existing.bank_bsb = None
            existing.bank_account_number = None
            existing.bank_account_title = None
            await session.commit()
            supplier_id = existing.id

    bill_id = await _post_bill(
        cid, supplier_id, expense, total=Decimal("10.00")
    )
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.PayRunError, match="missing ABA fields"):
            await svc.build_aba_from_selection(
                session,
                cid,
                bank_account_id=bank,
                selections=[
                    svc.PayRunSelection(
                        bill_id=bill_id, amount=Decimal("10.00")
                    )
                ],
                process_date=date(2026, 4, 21),
            )
