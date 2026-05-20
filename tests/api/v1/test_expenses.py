"""Contract tests for /api/v1/expenses.

Mirrors test_bills.py — same auth + locking + change_log pattern, plus
expense-specific checks: payment_account_id must be ASSET or LIABILITY;
contact_id is optional; status transitions DRAFT → POSTED produce a
balanced journal that credits the payment account (not AP).
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.change_log import ChangeLog
from saebooks.models.contact import Contact
from saebooks.models.journal import JournalEntry, JournalLine


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
async def unauth_client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture
async def expense_deps() -> dict[str, str]:
    """Return IDs needed to build an expense payload."""
    async with AsyncSessionLocal() as session:
        expense_acct = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.EXPENSE,
                    Account.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()
        # Find an ASSET account (bank / cash) to credit at checkout.
        # Most CoAs have at least one bank account seeded.
        payment_acct = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.ASSET,
                    Account.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()
        contact = (
            await session.execute(
                select(Contact).where(
                    Contact.archived_at.is_(None),
                    Contact.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()

    assert expense_acct is not None, "Test DB has no EXPENSE account in default tenant"
    assert payment_acct is not None, "Test DB has no ASSET account in default tenant"
    assert contact is not None, "Test DB has no contact in default tenant"
    return {
        "expense_account_id": str(expense_acct.id),
        "payment_account_id": str(payment_acct.id),
        "contact_id": str(contact.id),
    }


def _payload(deps: dict[str, str], **overrides: object) -> dict:
    base: dict = {
        "payment_account_id": deps["payment_account_id"],
        "contact_id": deps["contact_id"],
        "expense_date": "2026-04-01",
        "reference": "RECEIPT-12345",
        "notes": "Test expense",
        "lines": [
            {
                "description": "Fuel at Caltex",
                "account_id": deps["expense_account_id"],
                "quantity": "1",
                "unit_price": "120.00",
                "discount_pct": "0",
            },
        ],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_expenses_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/expenses")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_expenses_list_200(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/expenses")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)


async def test_expenses_list_filter_by_status(
    api_client: AsyncClient, expense_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/expenses", json=_payload(expense_deps))
    assert r.status_code == 201, r.text
    r2 = await api_client.get("/api/v1/expenses", params={"status": "DRAFT"})
    assert r2.status_code == 200
    for item in r2.json()["items"]:
        assert item["status"] == "DRAFT"


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


async def test_expenses_get_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/expenses/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_expenses_get_200_with_lines(
    api_client: AsyncClient, expense_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/expenses", json=_payload(expense_deps))
    assert r.status_code == 201, r.text
    expense_id = r.json()["id"]

    r2 = await api_client.get(f"/api/v1/expenses/{expense_id}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["id"] == expense_id
    assert len(body["lines"]) == 1
    assert body["lines"][0]["description"] == "Fuel at Caltex"


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_expenses_create_201(
    api_client: AsyncClient, expense_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/expenses", json=_payload(expense_deps))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["archived_at"] is None
    assert body["status"] == "DRAFT"
    assert body["payment_account_id"] == expense_deps["payment_account_id"]
    assert "tenant_id" in body
    assert float(body["subtotal"]) == 120.00


async def test_expenses_create_without_contact(
    api_client: AsyncClient, expense_deps: dict[str, str]
) -> None:
    """contact_id is optional — corner-shop receipt has no supplier on file."""
    payload = _payload(expense_deps)
    payload["contact_id"] = None
    r = await api_client.post("/api/v1/expenses", json=payload)
    assert r.status_code == 201, r.text
    assert r.json()["contact_id"] is None


async def test_expenses_reject_expense_account_as_payment(
    api_client: AsyncClient, expense_deps: dict[str, str]
) -> None:
    """payment_account_id must be ASSET or LIABILITY — not an EXPENSE account."""
    payload = _payload(
        expense_deps,
        payment_account_id=expense_deps["expense_account_id"],
    )
    r = await api_client.post("/api/v1/expenses", json=payload)
    assert r.status_code == 422
    assert "ASSET or LIABILITY" in r.text


async def test_expenses_create_change_log(
    api_client: AsyncClient, expense_deps: dict[str, str]
) -> None:
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/expenses", json=_payload(expense_deps))
    assert r.status_code == 201
    expense_id = r.json()["id"]

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(expense_id),
                    ChangeLog.entity == "expense",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert len(rows) == 1
    assert rows[0].op == "create"
    assert rows[0].version == 1


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


async def test_expenses_update_bumps_version(
    api_client: AsyncClient, expense_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/expenses", json=_payload(expense_deps))
    assert r.status_code == 201
    expense_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.patch(
        f"/api/v1/expenses/{expense_id}",
        json={"notes": "Updated notes"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    updated = r2.json()
    assert updated["version"] == v + 1
    assert updated["notes"] == "Updated notes"


async def test_expenses_update_requires_if_match(
    api_client: AsyncClient, expense_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/expenses", json=_payload(expense_deps))
    assert r.status_code == 201
    expense_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/expenses/{expense_id}", json={"notes": "x"}
    )
    assert r2.status_code == 428


async def test_expenses_stale_if_match_returns_409(
    api_client: AsyncClient, expense_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/expenses", json=_payload(expense_deps))
    assert r.status_code == 201
    expense_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/expenses/{expense_id}",
        json={"notes": "stale attempt"},
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == expense_id
    assert body["current"]["version"] == 1


# ---------------------------------------------------------------------------
# Delete (archive)
# ---------------------------------------------------------------------------


async def test_expenses_archive_204(
    api_client: AsyncClient, expense_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/expenses", json=_payload(expense_deps))
    assert r.status_code == 201
    expense_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.delete(
        f"/api/v1/expenses/{expense_id}",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 204

    r3 = await api_client.get("/api/v1/expenses")
    ids = [i["id"] for i in r3.json()["items"]]
    assert expense_id not in ids


async def test_expenses_delete_requires_if_match(
    api_client: AsyncClient, expense_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/expenses", json=_payload(expense_deps))
    assert r.status_code == 201
    expense_id = r.json()["id"]

    r2 = await api_client.delete(f"/api/v1/expenses/{expense_id}")
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# Post / void transitions
# ---------------------------------------------------------------------------


async def test_expenses_post_transitions_to_posted_with_je(
    api_client: AsyncClient, expense_deps: dict[str, str]
) -> None:
    """POST /{id}/post should mint a balanced journal entry that credits
    the payment account (NOT AP) and debits the expense account."""
    r = await api_client.post("/api/v1/expenses", json=_payload(expense_deps))
    assert r.status_code == 201
    expense_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/expenses/{expense_id}/post",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["status"] == "POSTED"
    assert body["journal_entry_id"] is not None
    assert body["number"] is not None
    assert body["number"].startswith("EX-")

    je_id = uuid.UUID(body["journal_entry_id"])
    payment_acct_id = uuid.UUID(expense_deps["payment_account_id"])
    async with AsyncSessionLocal() as session:
        lines = (
            await session.execute(
                select(JournalLine).where(JournalLine.entry_id == je_id)
            )
        ).scalars().all()

    # Journal must balance.
    debits = sum((ln.debit for ln in lines), Decimal("0"))
    credits = sum((ln.credit for ln in lines), Decimal("0"))
    assert debits == credits
    assert debits > Decimal("0")

    # Payment account is on the credit side, full total.
    payment_lines = [ln for ln in lines if ln.account_id == payment_acct_id]
    assert len(payment_lines) == 1
    assert payment_lines[0].credit == Decimal("120.00")
    assert payment_lines[0].debit == Decimal("0")


async def test_expenses_void_transitions_with_je_reversal(
    api_client: AsyncClient, expense_deps: dict[str, str]
) -> None:
    """Posted expense + void → status VOIDED + reversal journal entry."""
    r = await api_client.post("/api/v1/expenses", json=_payload(expense_deps))
    expense_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/expenses/{expense_id}/post",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    v = r2.json()["version"]

    r3 = await api_client.post(
        f"/api/v1/expenses/{expense_id}/void",
        headers={"If-Match": str(v)},
    )
    assert r3.status_code == 200, r3.text
    body = r3.json()
    assert body["status"] == "VOIDED"
    assert body["void_journal_entry_id"] is not None
    assert body["void_journal_entry_id"] != body["journal_entry_id"]


async def test_expenses_void_draft_no_je(
    api_client: AsyncClient, expense_deps: dict[str, str]
) -> None:
    """Voiding a DRAFT just flips status — no reversal JE needed."""
    r = await api_client.post("/api/v1/expenses", json=_payload(expense_deps))
    expense_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/expenses/{expense_id}/void",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["status"] == "VOIDED"
    assert body["journal_entry_id"] is None
    assert body["void_journal_entry_id"] is None
