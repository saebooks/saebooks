"""Hard-delete tests for invoices (gap ADMIN-DELETE-1).

* admin (X-Admin: true) can hard-delete with ?hard=true → 204
* non-admin (no X-Admin header) → 403
* audit_log row written with full snapshot
* row physically gone (not just archived)
* default DELETE (no ?hard) still soft-deletes — regression guard
"""
from __future__ import annotations

from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.audit_log import AuditLog
from saebooks.models.contact import Contact
from saebooks.models.invoice import Invoice


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
    async with AsyncSessionLocal() as session:
        income = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.INCOME,
                ).limit(1)
            )
        ).scalars().first()
        contact = (
            await session.execute(
                select(Contact).where(Contact.archived_at.is_(None)).limit(1)
            )
        ).scalars().first()
    assert income is not None
    assert contact is not None
    return {
        "income_account_id": str(income.id),
        "contact_id": str(contact.id),
    }


def _payload(deps: dict[str, str]) -> dict:
    return {
        "contact_id": deps["contact_id"],
        "issue_date": "2026-04-01",
        "due_date": "2026-05-01",
        "notes": "Hard-delete test",
        "lines": [
            {
                "description": "Test",
                "account_id": deps["income_account_id"],
                "quantity": "1",
                "unit_price": "100.00",
                "discount_pct": "0",
            },
        ],
    }


async def test_admin_can_hard_delete_invoice(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/invoices", json=_payload(invoice_deps))
    assert r.status_code == 201
    invoice_id = r.json()["id"]

    r2 = await api_client.delete(
        f"/api/v1/invoices/{invoice_id}?hard=true",
        headers={"X-Admin": "true"},
    )
    assert r2.status_code == 204

    async with AsyncSessionLocal() as s:
        row = await s.get(Invoice, invoice_id)
        assert row is None, "Invoice row should be physically gone"

        log = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.table_name == "invoices",
                    AuditLog.row_id == invoice_id,
                )
            )
        ).scalars().first()
        assert log is not None
        assert log.action == "hard_delete"
        assert log.row_snapshot["id"] == invoice_id
        assert "issue_date" in log.row_snapshot


async def test_non_admin_hard_delete_403(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/invoices", json=_payload(invoice_deps))
    assert r.status_code == 201
    invoice_id = r.json()["id"]

    r2 = await api_client.delete(f"/api/v1/invoices/{invoice_id}?hard=true")
    assert r2.status_code == 403


async def test_default_delete_still_soft_deletes(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/invoices", json=_payload(invoice_deps))
    assert r.status_code == 201
    invoice_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.delete(
        f"/api/v1/invoices/{invoice_id}",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 204

    async with AsyncSessionLocal() as s:
        row = await s.get(Invoice, invoice_id)
        assert row is not None, "Soft delete should leave row in place"
        assert row.archived_at is not None
