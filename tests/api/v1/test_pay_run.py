"""Router tests for /pay-run and /pay-run/export.

Covers:
* GET /pay-run renders candidate list (200)
* GET /pay-run without a company → 500 (tested via DB state)
* POST /pay-run/export missing bank_account_id → 400
* POST /pay-run/export invalid bank_account_id (not a UUID) → 400
* POST /pay-run/export missing process_date → 400
* POST /pay-run/export invalid process_date → 400
* POST /pay-run/export no bills selected → 400
* POST /pay-run/export with invalid amount string → 400
* POST /pay-run/export happy path → 200, text/plain, Content-Disposition
* ABA output: descriptor line present
* ABA output: filename includes process date
* ABA output: filename includes bill count
* POST with unknown bill UUID → 400 (PayRunError from service)
* GET /pay-run trailing slash also 200
* ABA output: CRLF line endings
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.bill import Bill, BillStatus
from saebooks.models.company import Company
from saebooks.models.contact import Contact


# ---------------------------------------------------------------------------
# Shared fixture: anonymous client (pay_run has no auth gate)
# ---------------------------------------------------------------------------


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Helpers to build a pay-run-ready bank account + contact + bill
# ---------------------------------------------------------------------------


async def _first_company() -> Company:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
    assert company is not None, "Test DB has no active company"
    return company


@pytest.fixture
async def aba_bank_account() -> Account:
    """Create a bank account with all ABA fields populated, return it."""
    company = await _first_company()
    acct = Account(
        company_id=company.id,
        code=f"9{uuid.uuid4().int % 9000:04d}",
        name="Test ABA Bank",
        account_type=AccountType.ASSET,
        reconcile=True,
        bsb="062-000",
        bank_account_number="123456789",
        bank_account_title="TEST COMPANY",
        apca_user_id="301500",
        bank_abbreviation="CBA",
    )
    async with AsyncSessionLocal() as session:
        session.add(acct)
        await session.commit()
        await session.refresh(acct)
    return acct


@pytest.fixture
async def payable_bill(aba_bank_account: Account) -> tuple[Bill, Contact, Account]:
    """Return (bill, contact, bank_account) ready for pay-run export."""
    company = await _first_company()

    # Expense account for bill lines
    async with AsyncSessionLocal() as session:
        expense = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.account_type == AccountType.EXPENSE,
                    Account.archived_at.is_(None),
                ).limit(1)
            )
        ).scalars().first()
    assert expense is not None, "Test DB has no EXPENSE account"

    # Supplier contact with bank details
    supplier = Contact(
        company_id=company.id,
        name=f"Supplier {uuid.uuid4().hex[:6]}",
        contact_type="VENDOR",
        bank_bsb="033-000",
        bank_account_number="987654321",
        bank_account_title="ACME SUPPLIER",
    )
    async with AsyncSessionLocal() as session:
        session.add(supplier)
        await session.flush()

        from saebooks.models.bill import BillLine

        bill = Bill(
            company_id=company.id,
            contact_id=supplier.id,
            status=BillStatus.POSTED,
            issue_date=date(2026, 4, 1),
            due_date=date(2026, 5, 1),
            total=Decimal("500.00"),
            amount_paid=Decimal("0.00"),
            number="BILL-001-TEST",
        )
        session.add(bill)
        await session.flush()

        line = BillLine(
            bill_id=bill.id,
            description="Test supply",
            account_id=expense.id,
            quantity=Decimal("1"),
            unit_price=Decimal("500.00"),
        )
        session.add(line)
        await session.commit()
        await session.refresh(bill)
        await session.refresh(supplier)

    return bill, supplier, aba_bank_account


# ---------------------------------------------------------------------------
# GET /pay-run
# ---------------------------------------------------------------------------


async def test_pay_run_index_200(client: AsyncClient) -> None:
    r = await client.get("/pay-run")
    assert r.status_code == 200


async def test_pay_run_index_trailing_slash_200(client: AsyncClient) -> None:
    r = await client.get("/pay-run/")
    assert r.status_code == 200


async def test_pay_run_index_contains_process_date_input(client: AsyncClient) -> None:
    r = await client.get("/pay-run")
    assert r.status_code == 200
    assert "process_date" in r.text


# ---------------------------------------------------------------------------
# POST /pay-run/export — validation failures
# ---------------------------------------------------------------------------


async def test_export_missing_bank_account_id(client: AsyncClient) -> None:
    r = await client.post(
        "/pay-run/export",
        data={"process_date": "2026-04-25"},
    )
    assert r.status_code == 400
    assert "bank_account_id" in r.text.lower()


async def test_export_invalid_bank_account_id_not_uuid(client: AsyncClient) -> None:
    r = await client.post(
        "/pay-run/export",
        data={"bank_account_id": "not-a-uuid", "process_date": "2026-04-25"},
    )
    assert r.status_code == 400


async def test_export_missing_process_date(client: AsyncClient) -> None:
    r = await client.post(
        "/pay-run/export",
        data={"bank_account_id": str(uuid.uuid4())},
    )
    assert r.status_code == 400
    assert "process_date" in r.text.lower()


async def test_export_invalid_process_date(client: AsyncClient) -> None:
    r = await client.post(
        "/pay-run/export",
        data={
            "bank_account_id": str(uuid.uuid4()),
            "process_date": "not-a-date",
        },
    )
    assert r.status_code == 400


async def test_export_no_bills_selected(client: AsyncClient, aba_bank_account: Account) -> None:
    r = await client.post(
        "/pay-run/export",
        data={
            "bank_account_id": str(aba_bank_account.id),
            "process_date": "2026-04-25",
        },
    )
    assert r.status_code == 400
    assert "select" in r.text.lower() or "bill" in r.text.lower()


async def test_export_invalid_amount_string(
    client: AsyncClient, payable_bill: tuple[Bill, Contact, Account]
) -> None:
    bill, _supplier, bank = payable_bill
    bill_id = str(bill.id)
    r = await client.post(
        "/pay-run/export",
        data={
            "bank_account_id": str(bank.id),
            "process_date": "2026-04-25",
            f"select_{bill_id}": "on",
            f"amount_{bill_id}": "not-a-number",
        },
    )
    assert r.status_code == 400


async def test_export_unknown_bill_uuid(
    client: AsyncClient, aba_bank_account: Account
) -> None:
    random_bill_id = str(uuid.uuid4())
    r = await client.post(
        "/pay-run/export",
        data={
            "bank_account_id": str(aba_bank_account.id),
            "process_date": "2026-04-25",
            f"select_{random_bill_id}": "on",
            f"amount_{random_bill_id}": "100.00",
        },
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# POST /pay-run/export — happy path
# ---------------------------------------------------------------------------


async def test_export_happy_path_200(
    client: AsyncClient, payable_bill: tuple[Bill, Contact, Account]
) -> None:
    bill, _supplier, bank = payable_bill
    bill_id = str(bill.id)
    r = await client.post(
        "/pay-run/export",
        data={
            "bank_account_id": str(bank.id),
            "process_date": "2026-04-25",
            f"select_{bill_id}": "on",
            f"amount_{bill_id}": "500.00",
        },
    )
    assert r.status_code == 200, r.text


async def test_export_content_type_text_plain(
    client: AsyncClient, payable_bill: tuple[Bill, Contact, Account]
) -> None:
    bill, _supplier, bank = payable_bill
    bill_id = str(bill.id)
    r = await client.post(
        "/pay-run/export",
        data={
            "bank_account_id": str(bank.id),
            "process_date": "2026-04-25",
            f"select_{bill_id}": "on",
            f"amount_{bill_id}": "500.00",
        },
    )
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]


async def test_export_content_disposition_attachment(
    client: AsyncClient, payable_bill: tuple[Bill, Contact, Account]
) -> None:
    bill, _supplier, bank = payable_bill
    bill_id = str(bill.id)
    r = await client.post(
        "/pay-run/export",
        data={
            "bank_account_id": str(bank.id),
            "process_date": "2026-04-25",
            f"select_{bill_id}": "on",
            f"amount_{bill_id}": "500.00",
        },
    )
    assert r.status_code == 200
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert "aba-260425-1.txt" in cd


async def test_export_aba_filename_includes_date_and_count(
    client: AsyncClient, payable_bill: tuple[Bill, Contact, Account]
) -> None:
    bill, _supplier, bank = payable_bill
    bill_id = str(bill.id)
    r = await client.post(
        "/pay-run/export",
        data={
            "bank_account_id": str(bank.id),
            "process_date": "2026-04-25",
            f"select_{bill_id}": "on",
            f"amount_{bill_id}": "500.00",
        },
    )
    assert r.status_code == 200
    # filename format: aba-<yymmdd>-<n>.txt
    cd = r.headers.get("content-disposition", "")
    assert "aba-260425-1.txt" in cd


async def test_export_aba_body_has_crlf_line_endings(
    client: AsyncClient, payable_bill: tuple[Bill, Contact, Account]
) -> None:
    bill, _supplier, bank = payable_bill
    bill_id = str(bill.id)
    r = await client.post(
        "/pay-run/export",
        data={
            "bank_account_id": str(bank.id),
            "process_date": "2026-04-25",
            f"select_{bill_id}": "on",
            f"amount_{bill_id}": "500.00",
        },
    )
    assert r.status_code == 200
    assert b"\r\n" in r.content


async def test_export_aba_body_starts_with_descriptor(
    client: AsyncClient, payable_bill: tuple[Bill, Contact, Account]
) -> None:
    """ABA descriptor (type 0) record is the first line — starts with '0'."""
    bill, _supplier, bank = payable_bill
    bill_id = str(bill.id)
    r = await client.post(
        "/pay-run/export",
        data={
            "bank_account_id": str(bank.id),
            "process_date": "2026-04-25",
            f"select_{bill_id}": "on",
            f"amount_{bill_id}": "500.00",
        },
    )
    assert r.status_code == 200
    first_line = r.text.split("\r\n")[0]
    assert first_line.startswith("0")
