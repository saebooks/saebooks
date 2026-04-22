"""Phase 1 contract tests for /api/v1/companies.

Covers:
* Auth gate (401 without bearer)
* List — returns active companies with version field
* Get — 200 (existing seed company), 404 for unknown UUID
* Update — PATCH with correct If-Match bumps version + appends change_log row
* Update — stale If-Match → 409
* Update — missing If-Match → 428
* change_log row appended on update
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
from saebooks.models.company import Company


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


async def _get_seed_company() -> tuple[str, int]:
    """Return (id, version) of the first active company in the test DB."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
        )
        company = result.scalars().first()
        if company is None:
            raise RuntimeError("No seed company in test DB — run alembic upgrade head first")
        return str(company.id), company.version


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_companies_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/companies")
    assert r.status_code == 401


async def test_companies_rejects_wrong_token(unauth_client: AsyncClient) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer totally-wrong"},
    ) as ac:
        r = await ac.get("/api/v1/companies")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_companies_list_200(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/companies")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert body["total"] >= 1
    # Every item must have a version field (Phase 1 requirement)
    for item in body["items"]:
        assert "version" in item
        assert isinstance(item["version"], int)
        assert item["version"] >= 1


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


async def test_companies_get_200(api_client: AsyncClient) -> None:
    company_id, _ = await _get_seed_company()
    r = await api_client.get(f"/api/v1/companies/{company_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == company_id
    assert "version" in body


async def test_companies_get_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/companies/{uuid.uuid4()}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Update — valid If-Match
# ---------------------------------------------------------------------------


async def test_companies_update_bumps_version(api_client: AsyncClient) -> None:
    company_id, version = await _get_seed_company()
    new_name = f"Updated Co {uuid.uuid4().hex[:6]}"
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"trading_name": new_name},
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == version + 1
    assert body["trading_name"] == new_name

    # Restore trading name for subsequent tests
    await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"trading_name": "SAE Engineering"},
        headers={"If-Match": str(version + 1)},
    )


# ---------------------------------------------------------------------------
# Update — missing If-Match → 428
# ---------------------------------------------------------------------------


async def test_companies_update_requires_if_match(api_client: AsyncClient) -> None:
    company_id, _ = await _get_seed_company()
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"trading_name": "should fail"},
    )
    assert r.status_code == 428


# ---------------------------------------------------------------------------
# Update — stale If-Match → 409
# ---------------------------------------------------------------------------


async def test_companies_stale_if_match_409(api_client: AsyncClient) -> None:
    company_id, _ = await _get_seed_company()
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"trading_name": "stale"},
        headers={"If-Match": "9999"},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == company_id


# ---------------------------------------------------------------------------
# change_log row appended on update
# ---------------------------------------------------------------------------


async def test_companies_change_log_on_update(api_client: AsyncClient) -> None:
    company_id, version = await _get_seed_company()

    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1))
        ).scalar_one_or_none() or 0

    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"trading_name": f"LogTest {uuid.uuid4().hex[:6]}"},
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 200

    new_version = r.json()["version"]

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(company_id),
                    ChangeLog.entity == "company",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert len(rows) >= 1
    assert rows[-1].op == "update"
    assert rows[-1].version == new_version

    # Restore version for next test
    await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"trading_name": "SAE Engineering"},
        headers={"If-Match": str(new_version)},
    )
