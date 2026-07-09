"""Phase 1 contract tests for /api/v1/accounts.

Covers:
* Auth gate (401 without bearer)
* List — all accounts, filter by account_type
* Get — 200, 404
* Create — 201, change_log row appended
* Update with correct If-Match — version bumped, change_log row appended
* Update with stale If-Match → 409 with current state in body
* Update without If-Match → 428
* Delete (soft-archive) — 204, archived_at set, version bumped
* Delete with stale If-Match → 409
* change_log rows: create + update + archive
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.change_log import ChangeLog

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
async def unauth_client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


def _rand_code(prefix: str = "6") -> str:
    """Generate a valid structured account code for the Expenses range (prefix 6).
    Uses 5 digits (max child levels) to provide 100 000 unique values and
    avoid collisions on a persistent shared DB across many test cycles.
    """
    raw = int(uuid.uuid4().hex[:5], 16)  # 0–1 048 575
    digits = str(raw % 100000).zfill(5)
    return f"{prefix}-{digits}"


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_accounts_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/accounts")
    assert r.status_code == 401


async def test_accounts_rejects_wrong_token(unauth_client: AsyncClient) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer totally-wrong"},
    ) as ac:
        r = await ac.get("/api/v1/accounts")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_accounts_list_returns_200(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/accounts")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)


async def test_accounts_list_filter_by_type(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/accounts", params={"account_type": "EXPENSE"})
    assert r.status_code == 200
    body = r.json()
    for item in body["items"]:
        assert item["account_type"] == "EXPENSE"


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


async def test_accounts_get_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/accounts/{uuid.uuid4()}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_accounts_create_201(api_client: AsyncClient) -> None:
    code = _rand_code()
    r = await api_client.post(
        "/api/v1/accounts",
        json={
            "code": code,
            "name": f"Test Account {code}",
            "account_type": "EXPENSE",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["code"] == code
    assert body["version"] == 1
    assert body["archived_at"] is None
    return body["id"]


async def test_accounts_get_after_create(api_client: AsyncClient) -> None:
    code = _rand_code()
    r = await api_client.post(
        "/api/v1/accounts",
        json={"code": code, "name": f"GetTest {code}", "account_type": "EXPENSE"},
    )
    assert r.status_code == 201
    account_id = r.json()["id"]

    r2 = await api_client.get(f"/api/v1/accounts/{account_id}")
    assert r2.status_code == 200
    assert r2.json()["id"] == account_id


# ---------------------------------------------------------------------------
# Update — valid If-Match
# ---------------------------------------------------------------------------


async def test_accounts_update_version_bumps(api_client: AsyncClient) -> None:
    code = _rand_code()
    r = await api_client.post(
        "/api/v1/accounts",
        json={"code": code, "name": f"UpdTest {code}", "account_type": "EXPENSE"},
    )
    assert r.status_code == 201
    aid = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.patch(
        f"/api/v1/accounts/{aid}",
        json={"name": f"Renamed {code}"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    updated = r2.json()
    assert updated["version"] == v + 1
    assert updated["name"] == f"Renamed {code}"


# ---------------------------------------------------------------------------
# Update — missing If-Match → 428
# ---------------------------------------------------------------------------


async def test_accounts_update_requires_if_match(api_client: AsyncClient) -> None:
    code = _rand_code()
    r = await api_client.post(
        "/api/v1/accounts",
        json={"code": code, "name": f"NoMatch {code}", "account_type": "EXPENSE"},
    )
    assert r.status_code == 201
    aid = r.json()["id"]

    r2 = await api_client.patch(f"/api/v1/accounts/{aid}", json={"name": "x"})
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# Update — stale If-Match → 409
# ---------------------------------------------------------------------------


async def test_accounts_stale_if_match_returns_409(api_client: AsyncClient) -> None:
    code = _rand_code()
    r = await api_client.post(
        "/api/v1/accounts",
        json={"code": code, "name": f"Stale {code}", "account_type": "EXPENSE"},
    )
    assert r.status_code == 201
    aid = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/accounts/{aid}",
        json={"name": "stale attempt"},
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == aid
    assert body["current"]["version"] == 1


# ---------------------------------------------------------------------------
# Soft-delete
# ---------------------------------------------------------------------------


async def test_accounts_soft_delete_204(api_client: AsyncClient) -> None:
    code = _rand_code()
    r = await api_client.post(
        "/api/v1/accounts",
        json={"code": code, "name": f"Del {code}", "account_type": "EXPENSE"},
    )
    assert r.status_code == 201
    aid = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.delete(
        f"/api/v1/accounts/{aid}",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 204

    # Should no longer appear in list
    r3 = await api_client.get("/api/v1/accounts")
    ids = [a["id"] for a in r3.json()["items"]]
    assert aid not in ids


async def test_accounts_delete_stale_if_match_409(api_client: AsyncClient) -> None:
    code = _rand_code()
    r = await api_client.post(
        "/api/v1/accounts",
        json={"code": code, "name": f"DelStale {code}", "account_type": "EXPENSE"},
    )
    assert r.status_code == 201
    aid = r.json()["id"]

    r2 = await api_client.delete(
        f"/api/v1/accounts/{aid}",
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409


# ---------------------------------------------------------------------------
# change_log rows: create + update + archive in order
# ---------------------------------------------------------------------------


async def test_accounts_change_log_on_writes(api_client: AsyncClient) -> None:
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1))
        ).scalar_one_or_none() or 0

    code = _rand_code()
    r = await api_client.post(
        "/api/v1/accounts",
        json={"code": code, "name": f"LogAcc {code}", "account_type": "EXPENSE"},
    )
    assert r.status_code == 201
    aid = r.json()["id"]

    await api_client.patch(
        f"/api/v1/accounts/{aid}",
        json={"name": f"LogAcc renamed {code}"},
        headers={"If-Match": "1"},
    )
    await api_client.delete(
        f"/api/v1/accounts/{aid}",
        headers={"If-Match": "2"},
    )

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(ChangeLog.id > before, ChangeLog.entity_id == uuid.UUID(aid))
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert [r.op for r in rows] == ["create", "update", "archive"]
    assert [r.version for r in rows] == [1, 2, 3]
    assert rows[0].entity == "account"
    assert rows[0].payload["code"] == code
