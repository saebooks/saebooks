"""Convert-to-inventory endpoint tests — POST /api/v1/fixed_assets/{id}/convert_to_inventory.

Gap MOTR-3: demonstrator vehicle conversion from FA register to inventory.

Tests:
* test_convert_to_inventory_happy      — 201, asset disposed, item created at NBV
* test_convert_active_no_depreciation  — asset with no_depreciation model converts correctly
* test_convert_already_disposed_422    — disposed asset → 422
* test_convert_stale_version_409       — stale If-Match → 409
* test_convert_missing_if_match_428    — absent If-Match → 428
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
from saebooks.models.item import Item
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
            "inventory_account_id": await _by_code("1-1330"),
            "cogs_account_id": await _by_code("5-2000"),
            "income_account_id": await _by_code("4-2000"),
        }


@pytest.fixture
async def gl() -> dict:
    return await _gl_accounts()


def _asset_payload(gl: dict, **overrides: object) -> dict:
    base: dict = {
        "name": "Demo Vehicle",
        "depreciation_model_id": "asset_5_year_linear",
        "purchase_date": "2024-01-01",
        "cost": "72000.00",
        "cost_account_id": gl["cost_account_id"],
        "accum_dep_account_id": gl["accum_dep_account_id"],
        "dep_expense_account_id": gl["dep_expense_account_id"],
    }
    base.update(overrides)
    return base


async def _create_asset(client: AsyncClient, gl: dict, **overrides: object) -> dict:
    r = await client.post("/api/v1/fixed_assets", json=_asset_payload(gl, **overrides))
    assert r.status_code == 201, r.text
    return r.json()


def _convert_payload(gl: dict, **overrides: object) -> dict:
    base: dict = {
        "conversion_date": "2026-04-28",
        "inventory_account_id": gl["inventory_account_id"],
        "cogs_account_id": gl["cogs_account_id"],
        "income_account_id": gl["income_account_id"],
    }
    base.update(overrides)
    return base


async def test_convert_to_inventory_happy(api_client: AsyncClient, gl: dict) -> None:
    """Active FA → POST /convert_to_inventory returns 201, asset disposed, item created."""
    import uuid as _uuid
    asset = await _create_asset(api_client, gl)
    asset_id = asset["id"]
    version = asset["version"]
    unique_sku = f"DEMO-{_uuid.uuid4().hex[:8].upper()}"

    r = await api_client.post(
        f"/api/v1/fixed_assets/{asset_id}/convert_to_inventory",
        json=_convert_payload(gl, sku=unique_sku, vin="1HGCM82633A123456"),
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 201, r.text
    body = r.json()

    # Asset should now be disposed.
    assert body["asset"]["status"] == "disposed"
    assert body["asset"]["id"] == asset_id
    assert body["asset"]["disposal_date"] == "2026-04-28"

    # NBV must be a sensible positive number.
    nbv = float(body["nbv"])
    assert nbv > 0, "NBV should be positive for an asset depreciated over a short period"
    assert float(body["asset"]["disposal_proceeds"]) == pytest.approx(nbv)

    # Item should be created.
    assert body["item_sku"] == unique_sku
    assert "item_id" in body
    assert "journal_id" in body

    # Verify item exists in DB with correct WAC.
    async with AsyncSessionLocal() as session:
        item = await session.get(Item, body["item_id"])
        assert item is not None
        assert item.sku == unique_sku
        assert float(item.on_hand_qty) == 1.0
        assert float(item.wac_cost) == pytest.approx(nbv, abs=0.01)
        assert item.description == "1HGCM82633A123456"


async def test_convert_active_no_depreciation(api_client: AsyncClient, gl: dict) -> None:
    """Asset with no_depreciation model: NBV equals cost, journal has no accum-dep line issue."""
    asset = await _create_asset(
        api_client,
        gl,
        depreciation_model_id="asset_no_depreciation",
        cost="50000.00",
    )
    asset_id = asset["id"]
    version = asset["version"]

    r = await api_client.post(
        f"/api/v1/fixed_assets/{asset_id}/convert_to_inventory",
        json=_convert_payload(gl),
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 201, r.text
    body = r.json()

    assert body["asset"]["status"] == "disposed"
    # No depreciation → NBV should equal original cost.
    assert float(body["nbv"]) == pytest.approx(50000.0)


async def test_convert_already_disposed_422(api_client: AsyncClient, gl: dict) -> None:
    """Attempting to convert a disposed asset returns 422."""
    asset = await _create_asset(api_client, gl)
    asset_id = asset["id"]
    version = asset["version"]

    # First conversion succeeds.
    r1 = await api_client.post(
        f"/api/v1/fixed_assets/{asset_id}/convert_to_inventory",
        json=_convert_payload(gl, sku=f"DEMO-DIS-{asset_id[:8]}"),
        headers={"If-Match": str(version)},
    )
    assert r1.status_code == 201, r1.text

    new_version = r1.json()["asset"]["version"]

    # Second conversion on already-disposed asset → 422.
    r2 = await api_client.post(
        f"/api/v1/fixed_assets/{asset_id}/convert_to_inventory",
        json=_convert_payload(gl),
        headers={"If-Match": str(new_version)},
    )
    assert r2.status_code == 422, r2.text


async def test_convert_stale_version_409(api_client: AsyncClient, gl: dict) -> None:
    """Stale If-Match version → 409 with current state in body."""
    asset = await _create_asset(api_client, gl)
    asset_id = asset["id"]
    version = asset["version"]

    # Bump the version via a PATCH.
    r_patch = await api_client.patch(
        f"/api/v1/fixed_assets/{asset_id}",
        json={"description": "Updated"},
        headers={"If-Match": str(version)},
    )
    assert r_patch.status_code == 200, r_patch.text

    # Now try to convert with the stale version.
    r = await api_client.post(
        f"/api/v1/fixed_assets/{asset_id}/convert_to_inventory",
        json=_convert_payload(gl),
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["version"] == version + 1


async def test_convert_missing_if_match_428(api_client: AsyncClient, gl: dict) -> None:
    """Missing If-Match header returns 428 Precondition Required."""
    asset = await _create_asset(api_client, gl)
    asset_id = asset["id"]

    r = await api_client.post(
        f"/api/v1/fixed_assets/{asset_id}/convert_to_inventory",
        json=_convert_payload(gl),
        # No If-Match header.
    )
    assert r.status_code == 428, r.text
