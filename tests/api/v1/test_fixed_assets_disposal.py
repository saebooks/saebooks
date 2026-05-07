"""Disposal endpoint tests — POST /api/v1/fixed_assets/{id}/dispose.

4 tests:
* test_dispose_happy
* test_dispose_already_disposed_422
* test_dispose_stale_version_409
* test_dispose_tenant_isolation
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account
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


async def _gl_accounts() -> dict:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None

        async def _by_code(code: str) -> str:
            acct = (
                await session.execute(
                    select(Account).where(
                        Account.company_id == company.id,
                        Account.code == code,
                    )
                )
            ).scalar_one()
            return str(acct.id)

        return {
            "cost_account_id": await _by_code("1-3310"),
            "accum_dep_account_id": await _by_code("1-3320"),
            "dep_expense_account_id": await _by_code("6-1500"),
        }


@pytest.fixture
async def gl() -> dict:
    return await _gl_accounts()


def _asset_payload(gl: dict, **overrides: object) -> dict:
    base: dict = {
        "name": "Disposal Test Asset",
        "depreciation_model_id": "asset_5_year_linear",
        "purchase_date": "2024-01-01",
        "cost": "5000.00",
        **gl,
    }
    base.update(overrides)
    return base


async def _create_active_asset(client: AsyncClient, gl: dict, **overrides: object) -> dict:
    r = await client.post("/api/v1/fixed_assets", json=_asset_payload(gl, **overrides))
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_dispose_happy(api_client: AsyncClient, gl: dict) -> None:
    """ACTIVE asset → POST /dispose returns 200 with status=disposed."""
    asset = await _create_active_asset(api_client, gl)
    asset_id = asset["id"]
    version = asset["version"]

    r = await api_client.post(
        f"/api/v1/fixed_assets/{asset_id}/dispose",
        json={
            "disposal_date": "2025-06-30",
            "proceeds": "4000.00",
            "notes": "Sold to buyer",
        },
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "disposed"
    assert body["id"] == asset_id
    assert body["version"] == version + 1
    assert body["disposal_date"] == "2025-06-30"
    assert float(body["disposal_proceeds"]) == 4000.0


async def test_dispose_already_disposed_422(api_client: AsyncClient, gl: dict) -> None:
    """Attempting to dispose an already-disposed asset returns 422."""
    asset = await _create_active_asset(api_client, gl)
    asset_id = asset["id"]
    version = asset["version"]

    # First disposal — should succeed.
    r1 = await api_client.post(
        f"/api/v1/fixed_assets/{asset_id}/dispose",
        json={"disposal_date": "2025-06-30", "proceeds": "1000.00"},
        headers={"If-Match": str(version)},
    )
    assert r1.status_code == 200, r1.text
    new_version = r1.json()["version"]

    # Second disposal on the same asset — must be rejected.
    r2 = await api_client.post(
        f"/api/v1/fixed_assets/{asset_id}/dispose",
        json={"disposal_date": "2025-07-01", "proceeds": "500.00"},
        headers={"If-Match": str(new_version)},
    )
    assert r2.status_code == 422, r2.text


async def test_dispose_stale_version_409(api_client: AsyncClient, gl: dict) -> None:
    """Stale If-Match version → 409 with current state in body."""
    asset = await _create_active_asset(api_client, gl)
    asset_id = asset["id"]
    version = asset["version"]

    # PATCH the asset to bump its version.
    r_patch = await api_client.patch(
        f"/api/v1/fixed_assets/{asset_id}",
        json={"description": "Updated description"},
        headers={"If-Match": str(version)},
    )
    assert r_patch.status_code == 200, r_patch.text
    # version is now version+1; the original version is stale.

    # Attempt dispose with the old (stale) version.
    r = await api_client.post(
        f"/api/v1/fixed_assets/{asset_id}/dispose",
        json={"disposal_date": "2025-06-30", "proceeds": "2000.00"},
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["detail"] == "version mismatch"
    assert "current" in body
    # Current version in body should be version+1.
    assert body["current"]["version"] == version + 1


async def test_dispose_tenant_isolation(api_client: AsyncClient, gl: dict) -> None:
    """Assets created under tenant A are stamped with that tenant's ID.

    The v1 API stores tenant_id on the asset row at creation time.
    This test verifies the disposed asset retains the original tenant_id
    rather than picking up a different caller's context.
    """
    from saebooks.api.v1.auth import DEFAULT_TENANT_ID

    asset = await _create_active_asset(api_client, gl)
    asset_id = asset["id"]
    version = asset["version"]
    original_tenant_id = asset["tenant_id"]

    # tenant_id on the created asset should match the default dev tenant.
    assert original_tenant_id == str(DEFAULT_TENANT_ID)

    # Dispose the asset — tenant_id must be preserved post-disposal.
    r = await api_client.post(
        f"/api/v1/fixed_assets/{asset_id}/dispose",
        json={"disposal_date": "2025-06-30", "proceeds": "2500.00"},
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "disposed"
    assert body["tenant_id"] == original_tenant_id, (
        "tenant_id must not change during disposal"
    )
