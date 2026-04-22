"""Phase 1 contract tests for /api/v1/tax_codes.

Covers:
* Auth gate (401 without bearer)
* List — all active tax codes, filter by tax_system
* Get — 200, 404
* Create — 201, change_log row appended, version=1
* Update with correct If-Match — version bumped, change_log row appended
* Update with stale If-Match → 409 with current state in body
* Update without If-Match → 428
* Delete (soft-archive) — 204, archived_at set, version bumped
* Delete with stale If-Match → 409
* change_log rows: create + update + archive in sequence
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


def _rand_code() -> str:
    """Generate a unique tax code abbreviation for test isolation."""
    return f"T{uuid.uuid4().hex[:4].upper()}"


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_tax_codes_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/tax_codes")
    assert r.status_code == 401


async def test_tax_codes_rejects_wrong_token(unauth_client: AsyncClient) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer totally-wrong"},
    ) as ac:
        r = await ac.get("/api/v1/tax_codes")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_tax_codes_list_200(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/tax_codes")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)


async def test_tax_codes_list_filter_by_tax_system(api_client: AsyncClient) -> None:
    # Create one with a custom tax_system to ensure we can filter
    code = _rand_code()
    await api_client.post(
        "/api/v1/tax_codes",
        json={
            "code": code,
            "name": f"Filter Test {code}",
            "rate": "5.000",
            "tax_system": "WET",
            "reporting_type": "taxable",
        },
    )
    r = await api_client.get("/api/v1/tax_codes", params={"tax_system": "WET"})
    assert r.status_code == 200
    body = r.json()
    for item in body["items"]:
        assert item["tax_system"] == "WET"
    codes = [i["code"] for i in body["items"]]
    assert code in codes


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


async def test_tax_codes_get_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/tax_codes/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_tax_codes_get_200(api_client: AsyncClient) -> None:
    code = _rand_code()
    r = await api_client.post(
        "/api/v1/tax_codes",
        json={"code": code, "name": f"Get Test {code}", "rate": "10.000"},
    )
    assert r.status_code == 201
    tc_id = r.json()["id"]

    r2 = await api_client.get(f"/api/v1/tax_codes/{tc_id}")
    assert r2.status_code == 200
    assert r2.json()["id"] == tc_id
    assert r2.json()["code"] == code


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_tax_codes_create_201(api_client: AsyncClient) -> None:
    code = _rand_code()
    r = await api_client.post(
        "/api/v1/tax_codes",
        json={
            "code": code,
            "name": f"Test Tax Code {code}",
            "rate": "10.000",
            "tax_system": "GST",
            "reporting_type": "taxable",
            "description": "Created by test",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["code"] == code
    assert body["version"] == 1
    assert body["archived_at"] is None
    assert body["tax_system"] == "GST"
    assert body["reporting_type"] == "taxable"


async def test_tax_codes_create_change_log(api_client: AsyncClient) -> None:
    """Create should produce a change_log row with op=create."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1))
        ).scalar_one_or_none() or 0

    code = _rand_code()
    r = await api_client.post(
        "/api/v1/tax_codes",
        json={"code": code, "name": f"CL Create {code}", "rate": "10.000"},
    )
    assert r.status_code == 201
    tc_id = r.json()["id"]

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(tc_id),
                    ChangeLog.entity == "tax_code",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert len(rows) == 1
    assert rows[0].op == "create"
    assert rows[0].version == 1
    assert rows[0].payload["code"] == code


# ---------------------------------------------------------------------------
# Update — valid If-Match
# ---------------------------------------------------------------------------


async def test_tax_codes_update_bumps_version(api_client: AsyncClient) -> None:
    code = _rand_code()
    r = await api_client.post(
        "/api/v1/tax_codes",
        json={"code": code, "name": f"Upd Test {code}", "rate": "10.000"},
    )
    assert r.status_code == 201
    tc_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.patch(
        f"/api/v1/tax_codes/{tc_id}",
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


async def test_tax_codes_update_requires_if_match(api_client: AsyncClient) -> None:
    code = _rand_code()
    r = await api_client.post(
        "/api/v1/tax_codes",
        json={"code": code, "name": f"NoMatch {code}", "rate": "10.000"},
    )
    assert r.status_code == 201
    tc_id = r.json()["id"]

    r2 = await api_client.patch(f"/api/v1/tax_codes/{tc_id}", json={"name": "x"})
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# Update — stale If-Match → 409
# ---------------------------------------------------------------------------


async def test_tax_codes_stale_if_match_returns_409(api_client: AsyncClient) -> None:
    code = _rand_code()
    r = await api_client.post(
        "/api/v1/tax_codes",
        json={"code": code, "name": f"Stale {code}", "rate": "10.000"},
    )
    assert r.status_code == 201
    tc_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/tax_codes/{tc_id}",
        json={"name": "stale attempt"},
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == tc_id
    assert body["current"]["version"] == 1


# ---------------------------------------------------------------------------
# Soft-delete
# ---------------------------------------------------------------------------


async def test_tax_codes_soft_delete_204(api_client: AsyncClient) -> None:
    code = _rand_code()
    r = await api_client.post(
        "/api/v1/tax_codes",
        json={"code": code, "name": f"Del {code}", "rate": "10.000"},
    )
    assert r.status_code == 201
    tc_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.delete(
        f"/api/v1/tax_codes/{tc_id}",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 204

    # Should no longer appear in list
    r3 = await api_client.get("/api/v1/tax_codes")
    ids = [tc["id"] for tc in r3.json()["items"]]
    assert tc_id not in ids


async def test_tax_codes_delete_stale_if_match_409(api_client: AsyncClient) -> None:
    code = _rand_code()
    r = await api_client.post(
        "/api/v1/tax_codes",
        json={"code": code, "name": f"DelStale {code}", "rate": "10.000"},
    )
    assert r.status_code == 201
    tc_id = r.json()["id"]

    r2 = await api_client.delete(
        f"/api/v1/tax_codes/{tc_id}",
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409


async def test_tax_codes_delete_requires_if_match(api_client: AsyncClient) -> None:
    code = _rand_code()
    r = await api_client.post(
        "/api/v1/tax_codes",
        json={"code": code, "name": f"DelNoMatch {code}", "rate": "10.000"},
    )
    assert r.status_code == 201
    tc_id = r.json()["id"]

    r2 = await api_client.delete(f"/api/v1/tax_codes/{tc_id}")
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# change_log rows: create + update + archive in order
# ---------------------------------------------------------------------------


async def test_tax_codes_change_log_on_writes(api_client: AsyncClient) -> None:
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1))
        ).scalar_one_or_none() or 0

    code = _rand_code()
    r = await api_client.post(
        "/api/v1/tax_codes",
        json={"code": code, "name": f"LogTC {code}", "rate": "10.000"},
    )
    assert r.status_code == 201
    tc_id = r.json()["id"]

    await api_client.patch(
        f"/api/v1/tax_codes/{tc_id}",
        json={"name": f"LogTC renamed {code}"},
        headers={"If-Match": "1"},
    )
    await api_client.delete(
        f"/api/v1/tax_codes/{tc_id}",
        headers={"If-Match": "2"},
    )

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(tc_id),
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert [row.op for row in rows] == ["create", "update", "archive"]
    assert [row.version for row in rows] == [1, 2, 3]
    assert rows[0].entity == "tax_code"
    assert rows[0].payload["code"] == code
