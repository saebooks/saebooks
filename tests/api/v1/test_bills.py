"""Phase 1 contract tests for /api/v1/bills.

Covers:
* Auth gate (401 without bearer)
* GET /api/v1/bills → 200 with pagination shape
* GET /api/v1/bills/{id} → 200 with lines; 404 on missing UUID
* POST /api/v1/bills → 201, version==1, change_log row created
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
pytestmark = pytest.mark.postgres_only


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
async def bill_deps() -> dict[str, str]:
    """Return IDs needed to build a bill payload."""
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
                select(Contact).where(
                    Contact.archived_at.is_(None),
                    Contact.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()

    assert expense is not None, "Test DB has no EXPENSE account in default tenant"
    assert contact is not None, "Test DB has no contact in default tenant"
    return {
        "expense_account_id": str(expense.id),
        "contact_id": str(contact.id),
    }


def _bill_payload(deps: dict[str, str], **overrides: object) -> dict:
    base: dict = {
        "contact_id": deps["contact_id"],
        "issue_date": "2026-04-01",
        "due_date": "2026-05-01",
        "notes": "Test bill",
        "lines": [
            {
                "description": "Office supplies",
                "account_id": deps["expense_account_id"],
                "quantity": "1",
                "unit_price": "200.00",
                "discount_pct": "0",
            },
        ],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_bills_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/bills")
    assert r.status_code == 401


async def test_bills_rejects_wrong_token(unauth_client: AsyncClient) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer totally-wrong"},
    ) as ac:
        r = await ac.get("/api/v1/bills")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_bills_list_200(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/bills")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)


async def test_bills_list_filter_by_status(
    api_client: AsyncClient, bill_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/bills", json=_bill_payload(bill_deps))
    assert r.status_code == 201

    r2 = await api_client.get("/api/v1/bills", params={"status": "DRAFT"})
    assert r2.status_code == 200
    for item in r2.json()["items"]:
        assert item["status"] == "DRAFT"


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


async def test_bills_get_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/bills/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_bills_get_200_with_lines(
    api_client: AsyncClient, bill_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/bills", json=_bill_payload(bill_deps))
    assert r.status_code == 201
    bill_id = r.json()["id"]

    r2 = await api_client.get(f"/api/v1/bills/{bill_id}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["id"] == bill_id
    assert "lines" in body
    assert len(body["lines"]) == 1
    assert body["lines"][0]["description"] == "Office supplies"


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_bills_create_201(
    api_client: AsyncClient, bill_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/bills", json=_bill_payload(bill_deps))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["archived_at"] is None
    assert body["status"] == "DRAFT"
    assert "tenant_id" in body
    assert len(body["lines"]) == 1
    # Subtotal should be 200.00
    assert float(body["subtotal"]) == 200.00


async def test_bills_create_change_log(
    api_client: AsyncClient, bill_deps: dict[str, str]
) -> None:
    """POST should produce a change_log row with op=create, version=1."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/bills", json=_bill_payload(bill_deps))
    assert r.status_code == 201
    bill_id = r.json()["id"]

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(bill_id),
                    ChangeLog.entity == "bill",
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


async def test_bills_update_bumps_version(
    api_client: AsyncClient, bill_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/bills", json=_bill_payload(bill_deps))
    assert r.status_code == 201
    bill_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.patch(
        f"/api/v1/bills/{bill_id}",
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


async def test_bills_update_requires_if_match(
    api_client: AsyncClient, bill_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/bills", json=_bill_payload(bill_deps))
    assert r.status_code == 201
    bill_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/bills/{bill_id}", json={"notes": "x"}
    )
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# Update — stale If-Match → 409
# ---------------------------------------------------------------------------


async def test_bills_stale_if_match_returns_409(
    api_client: AsyncClient, bill_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/bills", json=_bill_payload(bill_deps))
    assert r.status_code == 201
    bill_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/bills/{bill_id}",
        json={"notes": "stale attempt"},
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == bill_id
    assert body["current"]["version"] == 1


# ---------------------------------------------------------------------------
# Delete (void / soft-delete) → 204
# ---------------------------------------------------------------------------


async def test_bills_void_204(
    api_client: AsyncClient, bill_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/bills", json=_bill_payload(bill_deps))
    assert r.status_code == 201
    bill_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.delete(
        f"/api/v1/bills/{bill_id}",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 204

    # Should no longer appear in list (archived)
    r3 = await api_client.get("/api/v1/bills")
    ids = [i["id"] for i in r3.json()["items"]]
    assert bill_id not in ids


async def test_bills_delete_stale_if_match_409(
    api_client: AsyncClient, bill_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/bills", json=_bill_payload(bill_deps))
    assert r.status_code == 201
    bill_id = r.json()["id"]

    r2 = await api_client.delete(
        f"/api/v1/bills/{bill_id}",
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409


async def test_bills_delete_requires_if_match(
    api_client: AsyncClient, bill_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/bills", json=_bill_payload(bill_deps))
    assert r.status_code == 201
    bill_id = r.json()["id"]

    r2 = await api_client.delete(f"/api/v1/bills/{bill_id}")
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# change_log sequence
# ---------------------------------------------------------------------------


async def test_bills_change_log_create_update(
    api_client: AsyncClient, bill_deps: dict[str, str]
) -> None:
    """Create + update produces 2 change_log rows in order."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/bills", json=_bill_payload(bill_deps))
    assert r.status_code == 201
    bill_id = r.json()["id"]

    await api_client.patch(
        f"/api/v1/bills/{bill_id}",
        json={"notes": "updated"},
        headers={"If-Match": "1"},
    )

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(bill_id),
                    ChangeLog.entity == "bill",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert len(rows) == 2
    assert rows[0].op == "create"
    assert rows[0].version == 1
    assert rows[1].op == "update"
    assert rows[1].version == 2


async def test_bills_change_log_full_sequence(
    api_client: AsyncClient, bill_deps: dict[str, str]
) -> None:
    """Create + update + void = 3 change_log rows with versions 1, 2, 3."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/bills", json=_bill_payload(bill_deps))
    assert r.status_code == 201
    bill_id = r.json()["id"]

    await api_client.patch(
        f"/api/v1/bills/{bill_id}",
        json={"notes": "updated"},
        headers={"If-Match": "1"},
    )
    await api_client.delete(
        f"/api/v1/bills/{bill_id}",
        headers={"If-Match": "2"},
    )

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(bill_id),
                    ChangeLog.entity == "bill",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert [row.op for row in rows] == ["create", "update", "archive"]
    assert [row.version for row in rows] == [1, 2, 3]
    assert rows[0].entity == "bill"
