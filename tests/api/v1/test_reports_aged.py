"""Tier-5 report tests — /api/v1/reports/aged_receivables + /aged_payables.

10 tests total:
* test_aged_receivables_empty
* test_aged_receivables_current
* test_aged_receivables_30_day_bucket
* test_aged_receivables_90_plus
* test_aged_receivables_as_of_date
* test_aged_receivables_tenant_isolation
* test_aged_receivables_retentions_row  (CIVL-7)
* test_aged_payables_empty
* test_aged_payables_overdue
* test_aged_payables_tenant_isolation
"""
from __future__ import annotations

import os
import uuid
from datetime import date, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.fixture
async def invoice_deps() -> dict[str, str]:
    """Return account + contact IDs for building invoice payloads."""
    async with AsyncSessionLocal() as session:
        income = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.INCOME,
                    Account.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()
        contact = (
            await session.execute(
                select(Contact).where(Contact.archived_at.is_(None), Contact.tenant_id == DEFAULT_TENANT_ID).limit(1)
            )
        ).scalars().first()

    assert income is not None, "Test DB has no INCOME account"
    assert contact is not None, "Test DB has no contact"
    return {
        "income_account_id": str(income.id),
        "contact_id": str(contact.id),
    }


@pytest.fixture
async def bill_deps() -> dict[str, str]:
    """Return account + contact IDs for building bill payloads."""
    async with AsyncSessionLocal() as session:
        expense = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.EXPENSE,
                    Account.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()
        contact = (
            await session.execute(
                select(Contact).where(Contact.archived_at.is_(None), Contact.tenant_id == DEFAULT_TENANT_ID).limit(1)
            )
        ).scalars().first()

    assert expense is not None, "Test DB has no EXPENSE account"
    assert contact is not None, "Test DB has no contact"
    return {
        "expense_account_id": str(expense.id),
        "contact_id": str(contact.id),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _invoice_payload(
    deps: dict[str, str],
    issue_date: str,
    due_date: str,
    amount: str = "1000.00",
) -> dict:
    return {
        "contact_id": deps["contact_id"],
        "issue_date": issue_date,
        "due_date": due_date,
        "lines": [
            {
                "description": "Aged AR test line",
                "account_id": deps["income_account_id"],
                "quantity": "1",
                "unit_price": amount,
                "discount_pct": "0",
            }
        ],
    }


def _bill_payload(
    deps: dict[str, str],
    issue_date: str,
    due_date: str,
    amount: str = "800.00",
) -> dict:
    return {
        "contact_id": deps["contact_id"],
        "issue_date": issue_date,
        "due_date": due_date,
        "lines": [
            {
                "description": "Aged AP test line",
                "account_id": deps["expense_account_id"],
                "quantity": "1",
                "unit_price": amount,
                "discount_pct": "0",
            }
        ],
    }


async def _create_and_post_invoice(
    client: AsyncClient, deps: dict[str, str], issue_date: str, due_date: str
) -> dict:
    """Create a DRAFT invoice then transition it to POSTED. Return posted body."""
    r = await client.post(
        "/api/v1/invoices",
        json=_invoice_payload(deps, issue_date, due_date),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    inv_id = body["id"]
    version = body["version"]

    r2 = await client.post(
        f"/api/v1/invoices/{inv_id}/post",
        headers={"If-Match": str(version)},
    )
    assert r2.status_code == 200, r2.text
    return r2.json()


async def _create_and_post_bill(
    client: AsyncClient, deps: dict[str, str], issue_date: str, due_date: str
) -> dict:
    """Create a DRAFT bill then transition it to POSTED. Return posted body."""
    r = await client.post(
        "/api/v1/bills",
        json=_bill_payload(deps, issue_date, due_date),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    bill_id = body["id"]
    version = body["version"]

    r2 = await client.post(
        f"/api/v1/bills/{bill_id}/post",
        headers={"If-Match": str(version)},
    )
    assert r2.status_code == 200, r2.text
    return r2.json()


# ---------------------------------------------------------------------------
# Aged Receivables
# ---------------------------------------------------------------------------


async def test_aged_receivables_empty(api_client: AsyncClient) -> None:
    """With no open invoices the response is valid with empty contacts."""
    # Use a far-future as_of_date so any existing posted invoices (due in
    # the past) are still counted; actually use a far-past date so there
    # are certainly no invoices before the project started.
    r = await api_client.get(
        "/api/v1/reports/aged_receivables",
        params={"as_of_date": "2000-01-01"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["as_of_date"] == "2000-01-01"
    assert isinstance(body["buckets"], list)
    assert "current" in body["buckets"]
    # No invoices issued before 2000-01-01, so contacts must be empty
    assert body["contacts"] == []
    assert body["totals"]["total"] == 0.0


async def test_aged_receivables_current(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    """Invoice due tomorrow appears in the 'current' bucket."""
    today = date.today()
    tomorrow = today + timedelta(days=1)
    posted = await _create_and_post_invoice(
        api_client,
        invoice_deps,
        issue_date=today.isoformat(),
        due_date=tomorrow.isoformat(),
    )
    inv_id = posted["id"]

    r = await api_client.get(
        "/api/v1/reports/aged_receivables",
        params={"as_of_date": today.isoformat()},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # Find the contact row for our invoice
    contact_rows = [
        c for c in body["contacts"]
        if c["contact_id"] == invoice_deps["contact_id"]
    ]
    assert len(contact_rows) >= 1, "Contact row not found in AR report"
    row = contact_rows[0]
    assert row["current"] > 0, "Expected a positive current balance"
    assert row["1-30 days"] == 0.0


async def test_aged_receivables_30_day_bucket(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    """Invoice due in the past appears in the '1-30 days' bucket.

    We issue the invoice today and set due_date to today, then query with
    an as_of_date 20 days in the future — making the invoice 20 days
    overdue without needing a past issue_date that could hit a period lock.
    """
    today = date.today()
    as_of = today + timedelta(days=20)  # query 20 days forward → invoice is 20 days overdue

    await _create_and_post_invoice(
        api_client,
        invoice_deps,
        issue_date=today.isoformat(),
        due_date=today.isoformat(),
    )

    r = await api_client.get(
        "/api/v1/reports/aged_receivables",
        params={"as_of_date": as_of.isoformat()},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    contact_rows = [
        c for c in body["contacts"]
        if c["contact_id"] == invoice_deps["contact_id"]
    ]
    assert len(contact_rows) >= 1
    row = contact_rows[0]
    assert row["1-30 days"] > 0, "Expected a positive 1-30-days balance"


async def test_aged_receivables_90_plus(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    """Invoice appears in the '90+ days' bucket when queried 100 days after due.

    Issue today, set due_date to today, query 100 days forward so the
    invoice is 100 days overdue without crossing a period lock boundary.
    """
    today = date.today()
    as_of = today + timedelta(days=100)

    await _create_and_post_invoice(
        api_client,
        invoice_deps,
        issue_date=today.isoformat(),
        due_date=today.isoformat(),
    )

    r = await api_client.get(
        "/api/v1/reports/aged_receivables",
        params={"as_of_date": as_of.isoformat()},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    contact_rows = [
        c for c in body["contacts"]
        if c["contact_id"] == invoice_deps["contact_id"]
    ]
    assert len(contact_rows) >= 1
    row = contact_rows[0]
    assert row["90+ days"] > 0, "Expected a positive 90+ days balance"


async def test_aged_receivables_as_of_date(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    """The same invoice lands in different buckets depending on as_of_date.

    Issue and due today.  Query 40 days forward → 31-60 days bucket.
    Query as of today (due_date) → current bucket.
    """
    today = date.today()
    as_of_40_forward = today + timedelta(days=40)

    await _create_and_post_invoice(
        api_client,
        invoice_deps,
        issue_date=today.isoformat(),
        due_date=today.isoformat(),
    )

    # 40 days after due → 31-60 days bucket
    r1 = await api_client.get(
        "/api/v1/reports/aged_receivables",
        params={"as_of_date": as_of_40_forward.isoformat()},
    )
    body1 = r1.json()
    contact_rows1 = [
        c for c in body1["contacts"]
        if c["contact_id"] == invoice_deps["contact_id"]
    ]
    assert len(contact_rows1) >= 1
    assert contact_rows1[0]["31-60 days"] > 0

    # As of the due_date itself: days_overdue==0 → current
    r2 = await api_client.get(
        "/api/v1/reports/aged_receivables",
        params={"as_of_date": today.isoformat()},
    )
    body2 = r2.json()
    contact_rows2 = [
        c for c in body2["contacts"]
        if c["contact_id"] == invoice_deps["contact_id"]
    ]
    assert len(contact_rows2) >= 1
    assert contact_rows2[0]["current"] > 0


async def test_aged_receivables_tenant_isolation(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    """Tenant B cannot see tenant A's invoices in the AR report."""
    today = date.today()
    tomorrow = today + timedelta(days=1)

    # Create + post an invoice under the default tenant (A)
    await _create_and_post_invoice(
        api_client,
        invoice_deps,
        issue_date=today.isoformat(),
        due_date=tomorrow.isoformat(),
    )

    # Query the AR report as tenant B (random UUID)
    tenant_b_id = str(uuid.uuid4())
    original = os.environ.get("SAEBOOKS_DEV_TENANT_ID", "")
    os.environ["SAEBOOKS_DEV_TENANT_ID"] = tenant_b_id
    try:
        r = await api_client.get(
            "/api/v1/reports/aged_receivables",
            params={"as_of_date": today.isoformat()},
        )
    finally:
        if original:
            os.environ["SAEBOOKS_DEV_TENANT_ID"] = original
        else:
            os.environ.pop("SAEBOOKS_DEV_TENANT_ID", None)

    # Tenant B has no company → 404, which proves isolation.
    assert r.status_code in (200, 404), r.text
    if r.status_code == 200:
        body = r.json()
        contact_ids = [c["contact_id"] for c in body["contacts"]]
        assert invoice_deps["contact_id"] not in contact_ids, (
            "Tenant B should not see tenant A's AR"
        )


async def test_aged_receivables_retentions_row(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    """Retentions Receivable appears as a separate row; Trade Debtors is net.

    An invoice with 5% retention on a $1000 line (subtotal $1000, no GST
    for simplicity) should produce:
      - retentions_receivable total = $50 (5% of $1000)
      - contact trade debtor balance = $950
    This verifies CIVL-7: retentions are no longer buried in Trade Debtors.
    """
    today = date.today()
    tomorrow = today + timedelta(days=1)

    # Ensure the Retentions Receivable account (1-1220) exists for the company.
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None)).limit(1)
            )
        ).scalars().first()
        assert company is not None
        existing = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.code == "1-1220",
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(Account(
                company_id=company.id,
                code="1-1220",
                name="Retentions Receivable",
                account_type=AccountType.ASSET,
                reconcile=True,
                is_header=False,
            ))
            await session.commit()

    # Create + post an invoice with 5% retention on a $1,000 line.
    payload = {
        "contact_id": invoice_deps["contact_id"],
        "issue_date": today.isoformat(),
        "due_date": tomorrow.isoformat(),
        "lines": [
            {
                "description": "Civil progress claim — retention test",
                "account_id": invoice_deps["income_account_id"],
                "quantity": "1",
                "unit_price": "1000.00",
                "discount_pct": "0",
                "retention_pct": "5",
            }
        ],
    }
    r = await api_client.post("/api/v1/invoices", json=payload)
    assert r.status_code == 201, r.text
    inv_id = r.json()["id"]
    version = r.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/invoices/{inv_id}/post",
        headers={"If-Match": str(version)},
    )
    assert r2.status_code == 200, r2.text

    # Query AR aging report.
    r3 = await api_client.get(
        "/api/v1/reports/aged_receivables",
        params={"as_of_date": today.isoformat()},
    )
    assert r3.status_code == 200, r3.text
    body = r3.json()

    # retentions_receivable should be present and carry the 5% ($50).
    assert body["retentions_receivable"] is not None, (
        "Expected retentions_receivable row in AR aging (CIVL-7)"
    )
    assert body["retentions_receivable"]["total"] == pytest.approx(50.0, abs=0.01), (
        "Retentions Receivable total should be $50 (5% of $1000)"
    )

    # The contact's trade debtor balance should be $950 (not $1000).
    contact_rows = [
        c for c in body["contacts"]
        if c["contact_id"] == invoice_deps["contact_id"]
    ]
    assert len(contact_rows) >= 1, "Contact row not found in AR report"
    row = contact_rows[0]
    assert row["current"] == pytest.approx(950.0, abs=0.01), (
        "Trade Debtors should exclude the $50 retention (show $950)"
    )


# ---------------------------------------------------------------------------
# Aged Payables
# ---------------------------------------------------------------------------


async def test_aged_payables_empty(api_client: AsyncClient) -> None:
    """With no open bills the response is valid with empty contacts."""
    r = await api_client.get(
        "/api/v1/reports/aged_payables",
        params={"as_of_date": "2000-01-01"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["as_of_date"] == "2000-01-01"
    assert isinstance(body["buckets"], list)
    assert body["contacts"] == []
    assert body["totals"]["total"] == 0.0


async def test_aged_payables_overdue(
    api_client: AsyncClient, bill_deps: dict[str, str]
) -> None:
    """Bill appears in the '31-60 days' AP bucket when queried 45 days after due.

    Issue today, set due_date to today, query 45 days forward.
    """
    today = date.today()
    as_of = today + timedelta(days=45)

    await _create_and_post_bill(
        api_client,
        bill_deps,
        issue_date=today.isoformat(),
        due_date=today.isoformat(),
    )

    r = await api_client.get(
        "/api/v1/reports/aged_payables",
        params={"as_of_date": as_of.isoformat()},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    contact_rows = [
        c for c in body["contacts"]
        if c["contact_id"] == bill_deps["contact_id"]
    ]
    assert len(contact_rows) >= 1, "Contact row not found in AP report"
    row = contact_rows[0]
    assert row["31-60 days"] > 0, "Expected a positive 31-60-days AP balance"


async def test_aged_payables_tenant_isolation(
    api_client: AsyncClient, bill_deps: dict[str, str]
) -> None:
    """Tenant B cannot see tenant A's bills in the AP report."""
    today = date.today()
    tomorrow = today + timedelta(days=1)

    # Create + post a bill under the default tenant (A)
    await _create_and_post_bill(
        api_client,
        bill_deps,
        issue_date=today.isoformat(),
        due_date=tomorrow.isoformat(),
    )

    # Query the AP report as tenant B
    tenant_b_id = str(uuid.uuid4())
    original = os.environ.get("SAEBOOKS_DEV_TENANT_ID", "")
    os.environ["SAEBOOKS_DEV_TENANT_ID"] = tenant_b_id
    try:
        r = await api_client.get(
            "/api/v1/reports/aged_payables",
            params={"as_of_date": today.isoformat()},
        )
    finally:
        if original:
            os.environ["SAEBOOKS_DEV_TENANT_ID"] = original
        else:
            os.environ.pop("SAEBOOKS_DEV_TENANT_ID", None)

    # Tenant B has no company → 404, which proves isolation.
    assert r.status_code in (200, 404), r.text
    if r.status_code == 200:
        body = r.json()
        contact_ids = [c["contact_id"] for c in body["contacts"]]
        assert bill_deps["contact_id"] not in contact_ids, (
            "Tenant B should not see tenant A's AP"
        )
