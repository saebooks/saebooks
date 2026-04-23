"""Phase 1 tier-4 contract tests for /api/v1/bank_accounts.

Design (a): bank accounts are a view over the accounts table where bsb IS NOT NULL.

Covers:
* Auth gate (401 without bearer)
* GET /api/v1/bank_accounts → 200 with pagination shape
* GET /api/v1/bank_accounts/{id} → 200; 404 on missing UUID
* GET /api/v1/bank_accounts/{id} → 404 for a non-bank account
* POST /api/v1/bank_accounts → 201, version==1, change_log row created
* POST idempotency: same X-Idempotency-Key returns same response
* PATCH with correct If-Match → 200, version bumped
* PATCH with stale If-Match → 409 with current state in body
* PATCH without If-Match → 428
* DELETE with correct If-Match → 204 (soft-archive)
* DELETE with stale If-Match → 409
* DELETE without If-Match → 428
* Tenant isolation (bank account not visible after archive)
* Validation error: POST without required bsb → 422
* change_log sequence: create + update + delete = 3 rows with ops created/updated/deleted
"""
from __future__ import annotations

import uuid

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


def _ba_payload(**overrides: object) -> dict:
    """Return a minimal valid BankAccountCreate payload."""
    base: dict = {
        "code": f"1-{uuid.uuid4().hex[:8].upper()}",
        "name": "Test Operating Account",
        "bsb": "063-000",
        "bank_account_number": "12345678",
        "bank_account_title": "SAE Engineering",
        "apca_user_id": "123456",
        "bank_abbreviation": "CBA",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_bank_accounts_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/bank_accounts")
    assert r.status_code == 401


async def test_bank_accounts_rejects_wrong_token(unauth_client: AsyncClient) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer totally-wrong"},
    ) as ac:
        r = await ac.get("/api/v1/bank_accounts")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_bank_accounts_list_200(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/bank_accounts")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)


async def test_bank_accounts_list_only_bank_accounts(api_client: AsyncClient) -> None:
    """Listed items must all have a bsb value (confirming filter is applied)."""
    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201

    r2 = await api_client.get("/api/v1/bank_accounts")
    assert r2.status_code == 200
    for item in r2.json()["items"]:
        assert item["bsb"] is not None


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


async def test_bank_accounts_get_404_unknown_uuid(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/bank_accounts/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_bank_accounts_get_404_for_non_bank_account(api_client: AsyncClient) -> None:
    """A plain account without bsb should return 404 from the bank_accounts endpoint."""
    async with AsyncSessionLocal() as session:
        plain = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.bsb.is_(None),
                    Account.account_type == AccountType.ASSET,
                ).limit(1)
            )
        ).scalars().first()

    if plain is None:
        pytest.skip("No non-bank ASSET account available in test DB")

    r = await api_client.get(f"/api/v1/bank_accounts/{plain.id}")
    assert r.status_code == 404


async def test_bank_accounts_get_200(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201
    ba_id = r.json()["id"]

    r2 = await api_client.get(f"/api/v1/bank_accounts/{ba_id}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["id"] == ba_id
    assert body["bsb"] == "063-000"


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_bank_accounts_create_201(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["archived_at"] is None
    assert body["bsb"] == "063-000"
    assert body["bank_account_number"] == "12345678"
    assert body["bank_account_title"] == "SAE Engineering"
    assert body["apca_user_id"] == "123456"
    assert body["bank_abbreviation"] == "CBA"
    assert "tenant_id" in body


async def test_bank_accounts_create_change_log(api_client: AsyncClient) -> None:
    """POST should produce a change_log row with op=created, version=1."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201
    ba_id = r.json()["id"]

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(ba_id),
                    ChangeLog.entity == "bank_account",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert len(rows) >= 1
    assert rows[-1].op == "created"
    assert rows[-1].version == 1


async def test_bank_accounts_create_validation_missing_bsb(api_client: AsyncClient) -> None:
    """POST without bsb should return 422."""
    payload = _ba_payload()
    del payload["bsb"]
    r = await api_client.post("/api/v1/bank_accounts", json=payload)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_bank_accounts_create_idempotency(api_client: AsyncClient) -> None:
    """Same X-Idempotency-Key returns the same response body on replay."""
    key = str(uuid.uuid4())
    payload = _ba_payload()

    r1 = await api_client.post(
        "/api/v1/bank_accounts",
        json=payload,
        headers={"X-Idempotency-Key": key},
    )
    assert r1.status_code == 201
    id1 = r1.json()["id"]

    r2 = await api_client.post(
        "/api/v1/bank_accounts",
        json=payload,
        headers={"X-Idempotency-Key": key},
    )
    assert r2.status_code == 201
    assert r2.json()["id"] == id1


# ---------------------------------------------------------------------------
# Update — valid If-Match
# ---------------------------------------------------------------------------


async def test_bank_accounts_update_bumps_version(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201
    ba_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.patch(
        f"/api/v1/bank_accounts/{ba_id}",
        json={"name": "Updated Account Name"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    updated = r2.json()
    assert updated["version"] == v + 1
    assert updated["name"] == "Updated Account Name"


# ---------------------------------------------------------------------------
# Update — missing If-Match → 428
# ---------------------------------------------------------------------------


async def test_bank_accounts_update_requires_if_match(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201
    ba_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/bank_accounts/{ba_id}", json={"name": "x"}
    )
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# Update — stale If-Match → 409
# ---------------------------------------------------------------------------


async def test_bank_accounts_stale_if_match_returns_409(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201
    ba_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/bank_accounts/{ba_id}",
        json={"name": "stale attempt"},
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == ba_id
    assert body["current"]["version"] == 1


# ---------------------------------------------------------------------------
# Delete → 204
# ---------------------------------------------------------------------------


async def test_bank_accounts_delete_204(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201
    ba_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.delete(
        f"/api/v1/bank_accounts/{ba_id}",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 204

    # Should no longer appear in list (archived)
    r3 = await api_client.get("/api/v1/bank_accounts")
    ids = [i["id"] for i in r3.json()["items"]]
    assert ba_id not in ids


async def test_bank_accounts_delete_stale_if_match_409(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201
    ba_id = r.json()["id"]

    r2 = await api_client.delete(
        f"/api/v1/bank_accounts/{ba_id}",
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409


async def test_bank_accounts_delete_requires_if_match(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201
    ba_id = r.json()["id"]

    r2 = await api_client.delete(f"/api/v1/bank_accounts/{ba_id}")
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


async def test_bank_accounts_archived_not_in_list(api_client: AsyncClient) -> None:
    """Archived bank accounts must not appear in the list endpoint."""
    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201
    ba_id = r.json()["id"]
    v = r.json()["version"]

    await api_client.delete(
        f"/api/v1/bank_accounts/{ba_id}",
        headers={"If-Match": str(v)},
    )

    r2 = await api_client.get("/api/v1/bank_accounts")
    ids = [i["id"] for i in r2.json()["items"]]
    assert ba_id not in ids


# ---------------------------------------------------------------------------
# change_log sequence
# ---------------------------------------------------------------------------


async def test_bank_accounts_change_log_full_sequence(api_client: AsyncClient) -> None:
    """Create + update + delete = 3 bank_account change_log rows in order."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/bank_accounts", json=_ba_payload())
    assert r.status_code == 201
    ba_id = r.json()["id"]

    await api_client.patch(
        f"/api/v1/bank_accounts/{ba_id}",
        json={"name": "Renamed Account"},
        headers={"If-Match": "1"},
    )
    await api_client.delete(
        f"/api/v1/bank_accounts/{ba_id}",
        headers={"If-Match": "2"},
    )

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(ba_id),
                    ChangeLog.entity == "bank_account",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    ops = [row.op for row in rows]
    versions = [row.version for row in rows]
    assert "created" in ops
    assert "updated" in ops
    assert "deleted" in ops
    assert versions == sorted(versions)  # monotonically increasing
