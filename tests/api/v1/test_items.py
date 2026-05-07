"""Phase 1 contract tests for /api/v1/items.

Covers:
* Auth gate (401 without bearer)
* List — 200, optional item_type filter
* Get — 200, 404
* Create — 201, change_log row appended, version=1
* Update with correct If-Match — version bumped, change_log row appended
* Update with stale If-Match → 409 with current state in body
* Update without If-Match → 428
* Delete (soft-archive) — 204, archived_at set, version bumped
* Delete with stale If-Match → 409
* Delete without If-Match → 428
* change_log rows: create + update + archive in sequence
* Stock endpoint: 200 for inventory type, 404 for service type
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.change_log import ChangeLog


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
async def account_ids() -> dict[str, str]:
    """Return one ASSET, one EXPENSE, and one INCOME account ID as strings.

    Uses whatever accounts the test DB already has — the same DB that
    baseline tests run against, seeded during initial migration.
    """
    async with AsyncSessionLocal() as session:
        asset_row = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.ASSET,
                ).limit(1)
            )
        ).scalars().first()
        expense_row = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.EXPENSE,
                ).limit(1)
            )
        ).scalars().first()
        income_row = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.INCOME,
                ).limit(1)
            )
        ).scalars().first()

    assert asset_row is not None, "Test DB has no ASSET account"
    assert expense_row is not None, "Test DB has no EXPENSE account"
    assert income_row is not None, "Test DB has no INCOME account"

    return {
        "inventory_account_id": str(asset_row.id),
        "cogs_account_id": str(expense_row.id),
        "income_account_id": str(income_row.id),
    }


def _rand_sku() -> str:
    """Generate a unique SKU for test isolation."""
    return f"SKU-{uuid.uuid4().hex[:6].upper()}"


def _item_payload(account_ids: dict[str, str], **overrides: object) -> dict:
    sku = _rand_sku()
    base = {
        "sku": sku,
        "name": f"Test Item {sku}",
        "item_type": "inventory",
        "description": "Created by test",
        "cost_method": "WAC",
        "default_sale_price": "9.9900",
        "on_hand_qty": "0",
        "wac_cost": "0",
        **account_ids,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_items_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/items")
    assert r.status_code == 401


async def test_items_rejects_wrong_token(unauth_client: AsyncClient) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer totally-wrong"},
    ) as ac:
        r = await ac.get("/api/v1/items")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_items_list_200(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/items")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)


async def test_items_list_filter_by_item_type(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    # Create an inventory item and a service item
    r1 = await api_client.post(
        "/api/v1/items", json=_item_payload(account_ids, item_type="inventory")
    )
    assert r1.status_code == 201
    r2 = await api_client.post(
        "/api/v1/items", json=_item_payload(account_ids, item_type="service")
    )
    assert r2.status_code == 201

    r = await api_client.get("/api/v1/items", params={"item_type": "service", "limit": 1000})
    assert r.status_code == 200
    body = r.json()
    for item in body["items"]:
        assert item["item_type"] == "service"
    ids_in_list = [i["id"] for i in body["items"]]
    assert r2.json()["id"] in ids_in_list
    assert r1.json()["id"] not in ids_in_list


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


async def test_items_get_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/items/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_items_get_200(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    sku = _rand_sku()
    r = await api_client.post(
        "/api/v1/items",
        json=_item_payload(account_ids, sku=sku),
    )
    assert r.status_code == 201
    item_id = r.json()["id"]

    r2 = await api_client.get(f"/api/v1/items/{item_id}")
    assert r2.status_code == 200
    assert r2.json()["id"] == item_id
    assert r2.json()["sku"] == sku


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_items_create_201(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    sku = _rand_sku()
    r = await api_client.post(
        "/api/v1/items",
        json=_item_payload(account_ids, sku=sku),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["sku"] == sku
    assert body["version"] == 1
    assert body["archived_at"] is None
    assert body["item_type"] == "inventory"


async def test_items_create_change_log(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    """Create should produce a change_log row with op=create."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    sku = _rand_sku()
    r = await api_client.post(
        "/api/v1/items",
        json=_item_payload(account_ids, sku=sku),
    )
    assert r.status_code == 201
    item_id = r.json()["id"]

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(item_id),
                    ChangeLog.entity == "item",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert len(rows) == 1
    assert rows[0].op == "create"
    assert rows[0].version == 1
    assert rows[0].payload["sku"] == sku


# ---------------------------------------------------------------------------
# Update — valid If-Match
# ---------------------------------------------------------------------------


async def test_items_update_bumps_version(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/items", json=_item_payload(account_ids))
    assert r.status_code == 201
    item_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.patch(
        f"/api/v1/items/{item_id}",
        json={"name": "Renamed Item"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    updated = r2.json()
    assert updated["version"] == v + 1
    assert updated["name"] == "Renamed Item"


# ---------------------------------------------------------------------------
# Update — missing If-Match → 428
# ---------------------------------------------------------------------------


async def test_items_update_requires_if_match(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/items", json=_item_payload(account_ids))
    assert r.status_code == 201
    item_id = r.json()["id"]

    r2 = await api_client.patch(f"/api/v1/items/{item_id}", json={"name": "x"})
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# Update — stale If-Match → 409
# ---------------------------------------------------------------------------


async def test_items_stale_if_match_returns_409(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/items", json=_item_payload(account_ids))
    assert r.status_code == 201
    item_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/items/{item_id}",
        json={"name": "stale attempt"},
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == item_id
    assert body["current"]["version"] == 1


# ---------------------------------------------------------------------------
# Soft-delete
# ---------------------------------------------------------------------------


async def test_items_soft_delete_204(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/items", json=_item_payload(account_ids))
    assert r.status_code == 201
    item_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.delete(
        f"/api/v1/items/{item_id}",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 204

    # Should no longer appear in list
    r3 = await api_client.get("/api/v1/items")
    ids = [i["id"] for i in r3.json()["items"]]
    assert item_id not in ids


async def test_items_delete_stale_if_match_409(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/items", json=_item_payload(account_ids))
    assert r.status_code == 201
    item_id = r.json()["id"]

    r2 = await api_client.delete(
        f"/api/v1/items/{item_id}",
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409


async def test_items_delete_requires_if_match(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/items", json=_item_payload(account_ids))
    assert r.status_code == 201
    item_id = r.json()["id"]

    r2 = await api_client.delete(f"/api/v1/items/{item_id}")
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# change_log rows: create + update + archive in order
# ---------------------------------------------------------------------------


async def test_items_change_log_on_writes(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    sku = _rand_sku()
    r = await api_client.post(
        "/api/v1/items",
        json=_item_payload(account_ids, sku=sku),
    )
    assert r.status_code == 201
    item_id = r.json()["id"]

    await api_client.patch(
        f"/api/v1/items/{item_id}",
        json={"name": "Updated Item"},
        headers={"If-Match": "1"},
    )
    await api_client.delete(
        f"/api/v1/items/{item_id}",
        headers={"If-Match": "2"},
    )

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(item_id),
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert [row.op for row in rows] == ["create", "update", "archive"]
    assert [row.version for row in rows] == [1, 2, 3]
    assert rows[0].entity == "item"
    assert rows[0].payload["sku"] == sku


# ---------------------------------------------------------------------------
# Stock endpoint
# ---------------------------------------------------------------------------


async def test_items_stock_200_for_inventory(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    """Inventory-type item returns stock data."""
    r = await api_client.post(
        "/api/v1/items",
        json=_item_payload(account_ids, item_type="inventory"),
    )
    assert r.status_code == 201
    item_id = r.json()["id"]

    r2 = await api_client.get(f"/api/v1/items/{item_id}/stock")
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["item_id"] == item_id
    assert body["item_type"] == "inventory"
    assert "on_hand_qty" in body
    assert "wac_cost" in body
    assert "inventory_value" in body


async def test_items_stock_404_for_service(
    api_client: AsyncClient, account_ids: dict[str, str]
) -> None:
    """Service-type item returns 404 from stock endpoint."""
    r = await api_client.post(
        "/api/v1/items",
        json=_item_payload(account_ids, item_type="service"),
    )
    assert r.status_code == 201
    item_id = r.json()["id"]

    r2 = await api_client.get(f"/api/v1/items/{item_id}/stock")
    assert r2.status_code == 404


async def test_items_stock_404_for_unknown(api_client: AsyncClient) -> None:
    """Non-existent item returns 404 from stock endpoint."""
    r = await api_client.get(f"/api/v1/items/{uuid.uuid4()}/stock")
    assert r.status_code == 404
