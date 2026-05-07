"""Phase 1 contract tests for /api/v1/invoices.

Covers:
* Auth gate (401 without bearer)
* GET /api/v1/invoices → 200 with pagination shape
* GET /api/v1/invoices/{id} → 200 with lines; 404 on missing UUID
* POST /api/v1/invoices → 201, version==1, change_log row created
* PATCH with correct If-Match → 200, version bumped
* PATCH with stale If-Match → 409 with current state in body
* PATCH without If-Match → 428
* DELETE with correct If-Match → 204 (soft-void)
* DELETE with stale If-Match → 409
* DELETE without If-Match → 428
* change_log sequence: create + update = 2 rows; full sequence = 3 rows
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.change_log import ChangeLog
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
async def unauth_client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture
async def invoice_deps() -> dict[str, str]:
    """Return IDs needed to build an invoice payload."""
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
                select(Contact).where(
                    Contact.archived_at.is_(None),
                    Contact.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()

    assert income is not None, "Test DB has no INCOME account in default tenant"
    assert contact is not None, "Test DB has no contact in default tenant"
    return {
        "income_account_id": str(income.id),
        "contact_id": str(contact.id),
    }


def _invoice_payload(deps: dict[str, str], **overrides: object) -> dict:
    base: dict = {
        "contact_id": deps["contact_id"],
        "issue_date": "2026-04-01",
        "due_date": "2026-05-01",
        "notes": "Test invoice",
        "lines": [
            {
                "description": "Consulting services",
                "account_id": deps["income_account_id"],
                "quantity": "1",
                "unit_price": "500.00",
                "discount_pct": "0",
            },
        ],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_invoices_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/invoices")
    assert r.status_code == 401


async def test_invoices_rejects_wrong_token(unauth_client: AsyncClient) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer totally-wrong"},
    ) as ac:
        r = await ac.get("/api/v1/invoices")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_invoices_list_200(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/invoices")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)


async def test_invoices_list_filter_by_status(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/invoices", json=_invoice_payload(invoice_deps))
    assert r.status_code == 201

    r2 = await api_client.get("/api/v1/invoices", params={"status": "DRAFT"})
    assert r2.status_code == 200
    for item in r2.json()["items"]:
        assert item["status"] == "DRAFT"


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


async def test_invoices_get_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/invoices/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_invoices_get_200_with_lines(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/invoices", json=_invoice_payload(invoice_deps))
    assert r.status_code == 201
    invoice_id = r.json()["id"]

    r2 = await api_client.get(f"/api/v1/invoices/{invoice_id}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["id"] == invoice_id
    assert "lines" in body
    assert len(body["lines"]) == 1
    assert body["lines"][0]["description"] == "Consulting services"


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_invoices_create_201(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/invoices", json=_invoice_payload(invoice_deps))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["archived_at"] is None
    assert body["status"] == "DRAFT"
    assert "tenant_id" in body
    assert len(body["lines"]) == 1
    # Subtotal should be 500.00
    assert float(body["subtotal"]) == 500.00


async def test_invoices_create_change_log(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    """POST should produce a change_log row with op=create, version=1."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/invoices", json=_invoice_payload(invoice_deps))
    assert r.status_code == 201
    invoice_id = r.json()["id"]

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(invoice_id),
                    ChangeLog.entity == "invoice",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert len(rows) == 1
    assert rows[0].op == "create"
    assert rows[0].version == 1


# ---------------------------------------------------------------------------
# Update — valid If-Match
# ---------------------------------------------------------------------------


async def test_invoices_update_bumps_version(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/invoices", json=_invoice_payload(invoice_deps))
    assert r.status_code == 201
    invoice_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.patch(
        f"/api/v1/invoices/{invoice_id}",
        json={"notes": "Updated notes"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    updated = r2.json()
    assert updated["version"] == v + 1
    assert updated["notes"] == "Updated notes"


# ---------------------------------------------------------------------------
# Update — missing If-Match → 428
# ---------------------------------------------------------------------------


async def test_invoices_update_requires_if_match(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/invoices", json=_invoice_payload(invoice_deps))
    assert r.status_code == 201
    invoice_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/invoices/{invoice_id}", json={"notes": "x"}
    )
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# Update — stale If-Match → 409
# ---------------------------------------------------------------------------


async def test_invoices_stale_if_match_returns_409(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/invoices", json=_invoice_payload(invoice_deps))
    assert r.status_code == 201
    invoice_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/invoices/{invoice_id}",
        json={"notes": "stale attempt"},
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == invoice_id
    assert body["current"]["version"] == 1


# ---------------------------------------------------------------------------
# Delete (void / soft-delete) → 204
# ---------------------------------------------------------------------------


async def test_invoices_void_204(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/invoices", json=_invoice_payload(invoice_deps))
    assert r.status_code == 201
    invoice_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.delete(
        f"/api/v1/invoices/{invoice_id}",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 204

    # Should no longer appear in list (archived)
    r3 = await api_client.get("/api/v1/invoices")
    ids = [i["id"] for i in r3.json()["items"]]
    assert invoice_id not in ids


async def test_invoices_delete_stale_if_match_409(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/invoices", json=_invoice_payload(invoice_deps))
    assert r.status_code == 201
    invoice_id = r.json()["id"]

    r2 = await api_client.delete(
        f"/api/v1/invoices/{invoice_id}",
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409


async def test_invoices_delete_requires_if_match(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/invoices", json=_invoice_payload(invoice_deps))
    assert r.status_code == 201
    invoice_id = r.json()["id"]

    r2 = await api_client.delete(f"/api/v1/invoices/{invoice_id}")
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# change_log sequence
# ---------------------------------------------------------------------------


async def test_invoices_change_log_create_update(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    """Create + update produces 2 change_log rows in order."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/invoices", json=_invoice_payload(invoice_deps))
    assert r.status_code == 201
    invoice_id = r.json()["id"]

    await api_client.patch(
        f"/api/v1/invoices/{invoice_id}",
        json={"notes": "updated"},
        headers={"If-Match": "1"},
    )

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(invoice_id),
                    ChangeLog.entity == "invoice",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert len(rows) == 2
    assert rows[0].op == "create"
    assert rows[0].version == 1
    assert rows[1].op == "update"
    assert rows[1].version == 2


async def test_invoices_change_log_full_sequence(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    """Create + update + void = 3 change_log rows with versions 1, 2, 3."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/invoices", json=_invoice_payload(invoice_deps))
    assert r.status_code == 201
    invoice_id = r.json()["id"]

    await api_client.patch(
        f"/api/v1/invoices/{invoice_id}",
        json={"notes": "updated"},
        headers={"If-Match": "1"},
    )
    await api_client.delete(
        f"/api/v1/invoices/{invoice_id}",
        headers={"If-Match": "2"},
    )

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(invoice_id),
                    ChangeLog.entity == "invoice",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert [row.op for row in rows] == ["create", "update", "archive"]
    assert [row.version for row in rows] == [1, 2, 3]
    assert rows[0].entity == "invoice"
