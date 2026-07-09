"""Period-lock enforcement tests for invoices and bills (gap CIVL-4).

CIVL-4 (P1): invoices and bills dated inside a locked period were accepted
without warning. The fix adds a period-lock check to api_create() for both
invoices and bills, and translates PostingError → InvoiceError/BillError in
the post transition so the router returns 422 instead of 500.

Each test creates an isolated company with a period lock so it cannot
interfere with the shared default-company data used by other test modules.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.services import journal as journal_svc

pytestmark = pytest.mark.postgres_only

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# Period lock is at end of Q1 2026; tests use dates either side of it.
_LOCK_DATE = date(2026, 3, 31)
_DATE_INSIDE_LOCK = date(2026, 1, 15)
_DATE_AFTER_LOCK = date(2026, 4, 15)


# ---------------------------------------------------------------------------
# Shared setup helpers
# ---------------------------------------------------------------------------


async def _create_locked_company() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create an isolated company with a period lock + a contact + an income account.

    Returns (company_id, contact_id, income_account_id).
    """
    cid = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(Company(
            id=cid,
            tenant_id=_DEFAULT_TENANT_ID,
            name=f"CIVL4 Corp {cid.hex[:6]}",
        ))
        await session.flush()

        contact = Contact(
            company_id=cid,
            tenant_id=_DEFAULT_TENANT_ID,
            name="CIVL4 Customer",
            contact_type=ContactType.CUSTOMER,
        )
        income_acct = Account(
            company_id=cid,
            tenant_id=_DEFAULT_TENANT_ID,
            code=f"4-{cid.hex[:4]}",
            name="CIVL4 Income",
            account_type=AccountType.INCOME,
            is_header=False,
        )
        expense_acct = Account(
            company_id=cid,
            tenant_id=_DEFAULT_TENANT_ID,
            code=f"6-{cid.hex[:4]}",
            name="CIVL4 Expense",
            account_type=AccountType.EXPENSE,
            is_header=False,
        )
        session.add_all([contact, income_acct, expense_acct])
        await session.flush()

        await journal_svc.lock_period(
            session, cid, _LOCK_DATE, locked_by="test-civl4"
        )

        await session.refresh(contact)
        await session.refresh(income_acct)
        await session.refresh(expense_acct)
        contact_id = contact.id
        income_id = income_acct.id

    return cid, contact_id, income_id


async def _make_client(company_id: uuid.UUID) -> AsyncClient:
    token = current_token()
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Company-Id": str(company_id),
        },
    )


# ---------------------------------------------------------------------------
# CIVL-4: invoice create blocked when issue_date inside locked period
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoice_create_blocked_by_period_lock() -> None:
    """POST /invoices with issue_date inside locked period must return 422.

    Gap CIVL-4 (P1): before the fix api_create() had no period-lock check
    and accepted backdated invoice DRAFTs silently.
    """
    cid, contact_id, income_id = await _create_locked_company()

    async with await _make_client(cid) as client:
        payload = {
            "contact_id": str(contact_id),
            "issue_date": str(_DATE_INSIDE_LOCK),
            "due_date": "2026-02-15",
            "lines": [{
                "description": "CIVL-4 test line",
                "account_id": str(income_id),
                "quantity": "1",
                "unit_price": "100.00",
                "discount_pct": "0",
            }],
        }
        r = await client.post("/api/v1/invoices", json=payload)
        assert r.status_code == 422, (
            f"Expected 422 for invoice inside locked period, got {r.status_code}: {r.text}"
        )
        body = r.json()
        detail_str = str(body).lower()
        assert "lock" in detail_str or "period" in detail_str, (
            f"Expected 'lock' or 'period' in error body, got: {body}"
        )


# ---------------------------------------------------------------------------
# CIVL-4: bill create blocked when issue_date inside locked period
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bill_create_blocked_by_period_lock() -> None:
    """POST /bills with issue_date inside locked period must return 422.

    Gap CIVL-4 (P1): same guard applied to bills as per the finding.
    """
    cid, contact_id, expense_id = await _create_locked_company()

    async with await _make_client(cid) as client:
        payload = {
            "contact_id": str(contact_id),
            "issue_date": str(_DATE_INSIDE_LOCK),
            "due_date": "2026-02-15",
            "lines": [{
                "description": "CIVL-4 bill test line",
                "account_id": str(expense_id),
                "quantity": "1",
                "unit_price": "200.00",
                "discount_pct": "0",
            }],
        }
        r = await client.post("/api/v1/bills", json=payload)
        assert r.status_code == 422, (
            f"Expected 422 for bill inside locked period, got {r.status_code}: {r.text}"
        )
        body = r.json()
        detail_str = str(body).lower()
        assert "lock" in detail_str or "period" in detail_str, (
            f"Expected 'lock' or 'period' in error body, got: {body}"
        )


# ---------------------------------------------------------------------------
# CIVL-4: positive control — current-period invoice and bill accepted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoice_create_after_lock_accepted() -> None:
    """POST /invoices with issue_date after lock boundary must succeed (201).

    Positive control to confirm the guard does not over-block.
    """
    cid, contact_id, income_id = await _create_locked_company()

    async with await _make_client(cid) as client:
        payload = {
            "contact_id": str(contact_id),
            "issue_date": str(_DATE_AFTER_LOCK),
            "due_date": "2026-05-15",
            "lines": [{
                "description": "CIVL-4 positive control",
                "account_id": str(income_id),
                "quantity": "1",
                "unit_price": "100.00",
                "discount_pct": "0",
            }],
        }
        r = await client.post("/api/v1/invoices", json=payload)
        assert r.status_code == 201, (
            f"Expected 201 for current-period invoice, got {r.status_code}: {r.text}"
        )
        assert r.json()["status"] == "DRAFT"


@pytest.mark.asyncio
async def test_bill_create_after_lock_accepted() -> None:
    """POST /bills with issue_date after lock boundary must succeed (201).

    Positive control to confirm the bill guard does not over-block.
    """
    cid, contact_id, expense_id = await _create_locked_company()

    async with await _make_client(cid) as client:
        payload = {
            "contact_id": str(contact_id),
            "issue_date": str(_DATE_AFTER_LOCK),
            "due_date": "2026-05-15",
            "lines": [{
                "description": "CIVL-4 bill positive control",
                "account_id": str(expense_id),
                "quantity": "1",
                "unit_price": "200.00",
                "discount_pct": "0",
            }],
        }
        r = await client.post("/api/v1/bills", json=payload)
        assert r.status_code == 201, (
            f"Expected 201 for current-period bill, got {r.status_code}: {r.text}"
        )
        assert r.json()["status"] == "DRAFT"
