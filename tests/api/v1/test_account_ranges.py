"""Contract tests for /api/v1/account_ranges.

Covers:
* Auth gate (401 without bearer)
* List — 200 with items and total
* Create — 201, fields returned, appears in list
* Create with invalid prefix (non-numeric) — 422
* Create duplicate prefix — 422
* Update (PATCH) — 200, label changed
* Update non-existent — 404
* Delete — 204, no longer in list
* Delete non-existent — 404
* GET  /prefix_mode — 200
* PATCH /prefix_mode — 200, value updated
* PATCH /prefix_mode with invalid value — 422

NOTE: prefix "7" is deliberately absent from the default seeded ranges
(1-6, 8, 9) and is used for create/update/delete tests.  Each test that
creates a "7" range deletes it in teardown so tests are order-independent.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account_range import AccountRange
from saebooks.models.company import Company


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


async def _delete_prefix_if_exists(prefix: str) -> None:
    """Remove a range by prefix if it exists — for test isolation."""
    async with AsyncSessionLocal() as session:
        # Get the first company
        result = await session.execute(
            select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
        )
        company = result.scalars().first()
        if company is None:
            return
        result2 = await session.execute(
            select(AccountRange).where(
                AccountRange.company_id == company.id,
                AccountRange.prefix == prefix,
            )
        )
        rng = result2.scalars().first()
        if rng is not None:
            await session.delete(rng)
            await session.commit()


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_account_ranges_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/account_ranges")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_account_ranges_list_200(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/account_ranges")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)
    assert body["total"] == len(body["items"])


# ---------------------------------------------------------------------------
# Prefix mode get/set
# ---------------------------------------------------------------------------


async def test_account_ranges_prefix_mode_get(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/account_ranges/prefix_mode")
    assert r.status_code == 200
    body = r.json()
    assert "mode" in body
    assert body["mode"] in ("classic", "extended")


async def test_account_ranges_prefix_mode_set_extended(api_client: AsyncClient) -> None:
    r = await api_client.patch(
        "/api/v1/account_ranges/prefix_mode",
        json={"mode": "extended"},
    )
    assert r.status_code == 200
    assert r.json()["mode"] == "extended"

    # Verify it reads back
    r2 = await api_client.get("/api/v1/account_ranges/prefix_mode")
    assert r2.json()["mode"] == "extended"

    # Restore
    await api_client.patch(
        "/api/v1/account_ranges/prefix_mode", json={"mode": "classic"}
    )


async def test_account_ranges_prefix_mode_invalid(api_client: AsyncClient) -> None:
    r = await api_client.patch(
        "/api/v1/account_ranges/prefix_mode",
        json={"mode": "freeform"},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_account_ranges_create_201(api_client: AsyncClient) -> None:
    # prefix "7" is not in the default seeded ranges (1-6, 8, 9)
    await _delete_prefix_if_exists("7")

    r = await api_client.post(
        "/api/v1/account_ranges",
        json={
            "prefix": "7",
            "label": "Test Range Seven",
            "account_types": ["ASSET"],
            "sort_order": 99,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["prefix"] == "7"
    assert body["label"] == "Test Range Seven"
    assert body["account_types"] == ["ASSET"]
    assert body["sort_order"] == 99

    # Tidy up
    await api_client.delete(f"/api/v1/account_ranges/{body['id']}")


async def test_account_ranges_create_invalid_prefix_422(api_client: AsyncClient) -> None:
    r = await api_client.post(
        "/api/v1/account_ranges",
        json={
            "prefix": "abc",
            "label": "Bad Range",
            "account_types": ["ASSET"],
        },
    )
    assert r.status_code == 422


async def test_account_ranges_create_duplicate_prefix_422(api_client: AsyncClient) -> None:
    """Creating a range with an existing prefix returns 422."""
    await _delete_prefix_if_exists("7")

    r1 = await api_client.post(
        "/api/v1/account_ranges",
        json={
            "prefix": "7",
            "label": "First",
            "account_types": ["INCOME"],
        },
    )
    assert r1.status_code == 201, r1.text
    created_id = r1.json()["id"]

    r2 = await api_client.post(
        "/api/v1/account_ranges",
        json={
            "prefix": "7",
            "label": "Duplicate",
            "account_types": ["EXPENSE"],
        },
    )
    assert r2.status_code == 422

    # Tidy up
    await api_client.delete(f"/api/v1/account_ranges/{created_id}")


async def test_account_ranges_create_appears_in_list(api_client: AsyncClient) -> None:
    """Created range appears in GET /account_ranges."""
    await _delete_prefix_if_exists("7")

    r = await api_client.post(
        "/api/v1/account_ranges",
        json={
            "prefix": "7",
            "label": "List Check",
            "account_types": ["EXPENSE"],
            "sort_order": 0,
        },
    )
    assert r.status_code == 201, r.text
    created_id = r.json()["id"]

    r2 = await api_client.get("/api/v1/account_ranges")
    ids = [rng["id"] for rng in r2.json()["items"]]
    assert created_id in ids

    # Tidy up
    await api_client.delete(f"/api/v1/account_ranges/{created_id}")


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


async def test_account_ranges_update_200(api_client: AsyncClient) -> None:
    await _delete_prefix_if_exists("7")

    r = await api_client.post(
        "/api/v1/account_ranges",
        json={
            "prefix": "7",
            "label": "Original Label",
            "account_types": ["INCOME"],
            "sort_order": 0,
        },
    )
    assert r.status_code == 201, r.text
    range_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/account_ranges/{range_id}",
        json={"label": "Updated Label", "sort_order": 55},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["label"] == "Updated Label"
    assert body["sort_order"] == 55

    # Tidy up
    await api_client.delete(f"/api/v1/account_ranges/{range_id}")


async def test_account_ranges_update_not_found(api_client: AsyncClient) -> None:
    r = await api_client.patch(
        f"/api/v1/account_ranges/{uuid.uuid4()}",
        json={"label": "ghost"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def test_account_ranges_delete_204(api_client: AsyncClient) -> None:
    await _delete_prefix_if_exists("7")

    r = await api_client.post(
        "/api/v1/account_ranges",
        json={
            "prefix": "7",
            "label": "To Delete",
            "account_types": ["EXPENSE"],
        },
    )
    assert r.status_code == 201, r.text
    range_id = r.json()["id"]

    r2 = await api_client.delete(f"/api/v1/account_ranges/{range_id}")
    assert r2.status_code == 204

    # Should no longer appear in list
    r3 = await api_client.get("/api/v1/account_ranges")
    ids = [rng["id"] for rng in r3.json()["items"]]
    assert range_id not in ids


async def test_account_ranges_delete_not_found(api_client: AsyncClient) -> None:
    r = await api_client.delete(f"/api/v1/account_ranges/{uuid.uuid4()}")
    assert r.status_code == 404
