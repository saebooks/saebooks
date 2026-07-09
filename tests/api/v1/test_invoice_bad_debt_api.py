"""Contract tests for the bad-debt write-off + recovery endpoints.

* POST /api/v1/invoices/{id}/write-off → 200, status WRITTEN_OFF; re-call 409.
* write-off on a never-posted (DRAFT) invoice → 422.
* POST /api/v1/invoices/{id}/record-recovery → 201 (Dr bank / Cr Other Income).
* record-recovery on a non-written-off invoice → 409.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.contact import Contact

pytestmark = pytest.mark.postgres_only


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
async def deps() -> dict[str, str]:
    async with AsyncSessionLocal() as session:
        income = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.INCOME,
                    Account.is_header.is_(False),
                    Account.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()
        bank = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.ASSET,
                    Account.is_header.is_(False),
                    Account.code.like("1-11%"),
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
        assert income is not None and bank is not None and contact is not None
        return {
            "income_account_id": str(income.id),
            "bank_account_id": str(bank.id),
            "contact_id": str(contact.id),
        }


async def _create_posted_invoice(client: AsyncClient, deps: dict[str, str]) -> str:
    """Create + post a GST-free invoice; return its id."""
    payload = {
        "contact_id": deps["contact_id"],
        "issue_date": "2026-04-01",
        "due_date": "2026-05-01",
        "lines": [
            {
                "description": "Uncollectable job",
                "account_id": deps["income_account_id"],
                "quantity": "1",
                "unit_price": "300.00",
                "discount_pct": "0",
            }
        ],
    }
    r = await client.post("/api/v1/invoices", json=payload)
    assert r.status_code == 201, r.text
    inv = r.json()
    inv_id = inv["id"]
    # Post it (DRAFT -> POSTED) — requires If-Match with current version.
    r2 = await client.post(
        f"/api/v1/invoices/{inv_id}/post",
        headers={"If-Match": str(inv["version"])},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "POSTED"
    return inv_id


async def test_write_off_then_409_on_recall(
    api_client: AsyncClient, deps: dict[str, str]
) -> None:
    inv_id = await _create_posted_invoice(api_client, deps)

    r = await api_client.post(
        f"/api/v1/invoices/{inv_id}/write-off",
        json={"write_off_date": "2026-06-23", "reason": "uncollectable"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "WRITTEN_OFF"
    assert body["amount_paid"] == body["total"]

    # Re-call → 409 (already written off).
    r2 = await api_client.post(
        f"/api/v1/invoices/{inv_id}/write-off",
        json={"write_off_date": "2026-06-23"},
    )
    assert r2.status_code == 409, r2.text


async def test_write_off_404_on_missing(
    api_client: AsyncClient, deps: dict[str, str]
) -> None:
    r = await api_client.post(
        f"/api/v1/invoices/{uuid.uuid4()}/write-off", json={}
    )
    assert r.status_code == 404


async def test_write_off_422_on_draft(
    api_client: AsyncClient, deps: dict[str, str]
) -> None:
    # Create a DRAFT invoice but do NOT post it.
    payload = {
        "contact_id": deps["contact_id"],
        "issue_date": "2026-04-01",
        "due_date": "2026-05-01",
        "lines": [
            {
                "description": "Draft job",
                "account_id": deps["income_account_id"],
                "quantity": "1",
                "unit_price": "100.00",
            }
        ],
    }
    r = await api_client.post("/api/v1/invoices", json=payload)
    inv_id = r.json()["id"]
    r2 = await api_client.post(
        f"/api/v1/invoices/{inv_id}/write-off", json={}
    )
    assert r2.status_code == 422, r2.text


async def test_record_recovery_201(
    api_client: AsyncClient, deps: dict[str, str]
) -> None:
    inv_id = await _create_posted_invoice(api_client, deps)
    # Write it off first.
    r = await api_client.post(
        f"/api/v1/invoices/{inv_id}/write-off", json={"write_off_date": "2026-06-23"}
    )
    assert r.status_code == 200

    # Record a partial recovery.
    r2 = await api_client.post(
        f"/api/v1/invoices/{inv_id}/record-recovery",
        json={
            "bank_account_id": deps["bank_account_id"],
            "amount": "120.00",
            "recovery_date": "2026-07-01",
        },
    )
    assert r2.status_code == 201, r2.text
    body = r2.json()
    assert body["invoice_id"] == inv_id
    assert body["amount"] == "120.00"
    assert body["journal_entry_id"]


async def test_record_recovery_409_on_non_written_off(
    api_client: AsyncClient, deps: dict[str, str]
) -> None:
    # A POSTED (not written off) invoice cannot be recovered.
    inv_id = await _create_posted_invoice(api_client, deps)
    r = await api_client.post(
        f"/api/v1/invoices/{inv_id}/record-recovery",
        json={
            "bank_account_id": deps["bank_account_id"],
            "amount": "50.00",
            "recovery_date": "2026-07-01",
        },
    )
    assert r.status_code == 409, r.text
