"""Tier-5 depreciation schedule report tests.

GET /api/v1/reports/depreciation_schedule

5 tests:
* test_depreciation_schedule_empty
* test_depreciation_schedule_straight_line
* test_depreciation_schedule_declining_balance
* test_depreciation_schedule_fully_depreciated
* test_depreciation_schedule_tenant_isolation
"""
from __future__ import annotations

import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account
from saebooks.models.company import Company

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


@pytest.fixture(autouse=True)
def _set_edition_enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run this file's tests at enterprise (all flags on) by default.

    test_depreciation_schedule_declining_balance below creates a real
    fixed asset on a diminishing-value model (``asset_dv_40``) over
    HTTP — FLAG_ASSET_V2 (Wave A, 2026-07-10) now gates that selection
    on create. The other tests use linear models, which stay ungated
    at every tier, so this is a harmless no-op for them.
    """
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "enterprise")


async def _gl_accounts() -> dict:
    """Return the three GL account IDs every asset test needs."""
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
        "name": "DepSched Test Asset",
        "depreciation_model_id": "asset_3_year_linear",
        "purchase_date": "2024-01-01",
        "cost": "3600.00",
        **gl,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_asset(client: AsyncClient, gl: dict, **overrides: object) -> dict:
    r = await client.post("/api/v1/fixed_assets", json=_asset_payload(gl, **overrides))
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_depreciation_schedule_empty(api_client: AsyncClient, gl: dict) -> None:
    """Schedule endpoint returns correct structure; disposed assets are excluded.

    The DB may contain assets from other tests, so we can't assert zero totals.
    We instead verify the response shape and that a disposed asset is absent.
    """
    # Create an asset then dispose it so we have something to check exclusion for.
    asset = await _create_asset(
        api_client,
        gl,
        cost="999.00",
        purchase_date="2025-01-01",
    )
    asset_id = asset["id"]
    version = asset["version"]

    # Dispose the asset via the dispose endpoint.
    r_dispose = await api_client.post(
        f"/api/v1/fixed_assets/{asset_id}/dispose",
        json={"disposal_date": "2025-03-01", "proceeds": "500.00"},
        headers={"If-Match": str(version)},
    )
    assert r_dispose.status_code == 200, r_dispose.text

    # Disposed asset must NOT appear in the depreciation schedule.
    r = await api_client.get("/api/v1/reports/depreciation_schedule")
    assert r.status_code == 200, r.text
    body = r.json()

    assert "as_of_date" in body
    assert "assets" in body
    assert isinstance(body["assets"], list)
    assert "total_cost" in body
    assert "total_accumulated" in body
    assert "total_book_value" in body

    ids = [a["asset_id"] for a in body["assets"]]
    assert asset_id not in ids, "Disposed asset must not appear in depreciation schedule"


async def test_depreciation_schedule_straight_line(
    api_client: AsyncClient, gl: dict
) -> None:
    """Linear (straight-line) asset: next_month calc = (cost-residual) / months."""
    # cost=3600, residual=0, 3-year linear = 36 months
    # next_month_depreciation = 3600 / 36 = 100.00
    asset = await _create_asset(
        api_client,
        gl,
        depreciation_model_id="asset_3_year_linear",
        cost="3600.00",
        residual_value="0.00",
        purchase_date="2025-01-01",
    )
    asset_id = asset["id"]

    r = await api_client.get(
        "/api/v1/reports/depreciation_schedule",
        params={"as_of_date": "2025-06-30"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    matching = [a for a in body["assets"] if a["asset_id"] == asset_id]
    assert len(matching) == 1, f"Asset {asset_id} not found in schedule"
    line = matching[0]

    assert line["depreciation_method"] == "linear"
    assert line["cost"] == 3600.0
    assert line["residual_value"] == 0.0
    assert line["useful_life_months"] == 36
    assert line["fully_depreciated"] is False
    # next_month should be 3600/36 = 100.00 (within rounding tolerance)
    assert abs(line["next_month_depreciation"] - 100.0) < 0.02
    # accumulated > 0 (asset has been in service for ~6 months)
    assert line["accumulated_depreciation"] > 0.0
    # book value = cost - accumulated
    assert abs(
        line["current_book_value"]
        - (line["cost"] - line["accumulated_depreciation"])
    ) < 0.02


async def test_depreciation_schedule_declining_balance(
    api_client: AsyncClient, gl: dict
) -> None:
    """DV asset: next_month = book_value * (rate/100/12)."""
    # asset_dv_40 = 40% p.a. DV
    # next_month at book_value B = B * 0.40 / 12 = B * 0.03333...
    asset = await _create_asset(
        api_client,
        gl,
        depreciation_model_id="asset_dv_40",
        cost="12000.00",
        residual_value="0.00",
        purchase_date="2025-01-01",
    )
    asset_id = asset["id"]

    r = await api_client.get(
        "/api/v1/reports/depreciation_schedule",
        params={
            "as_of_date": "2025-06-30",
            "method": "DECLINING_BALANCE",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()

    matching = [a for a in body["assets"] if a["asset_id"] == asset_id]
    assert len(matching) == 1, f"DV asset {asset_id} not found in schedule"
    line = matching[0]

    assert line["depreciation_method"] == "diminishing_value"
    assert line["fully_depreciated"] is False
    bv = line["current_book_value"]
    expected_next = round(bv * 40.0 / 100 / 12, 2)
    assert abs(line["next_month_depreciation"] - expected_next) < 0.05


async def test_depreciation_schedule_fully_depreciated(
    api_client: AsyncClient, gl: dict
) -> None:
    """Fully depreciated asset: fully_depreciated=True, next_month=0."""
    # Create an asset with very short life (3-year linear but look far in the future)
    asset = await _create_asset(
        api_client,
        gl,
        depreciation_model_id="asset_3_year_linear",
        cost="1200.00",
        residual_value="0.00",
        purchase_date="2020-01-01",
    )
    asset_id = asset["id"]

    # as_of_date = 2024-01-01 — well past the 3-year useful life from 2020
    r = await api_client.get(
        "/api/v1/reports/depreciation_schedule",
        params={"as_of_date": "2024-01-01"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    matching = [a for a in body["assets"] if a["asset_id"] == asset_id]
    assert len(matching) == 1, f"Asset {asset_id} not found in schedule"
    line = matching[0]

    assert line["fully_depreciated"] is True
    assert line["next_month_depreciation"] == 0.0
    # accumulated should equal full cost (residual=0)
    assert abs(line["accumulated_depreciation"] - 1200.0) < 0.02
    assert abs(line["current_book_value"]) < 0.02


async def test_depreciation_schedule_tenant_isolation(
    api_client: AsyncClient, gl: dict
) -> None:
    """Assets created by this tenant only appear for this tenant."""
    # Create an asset with a distinctive cost for identification
    distinctive_cost = "7777.00"
    asset = await _create_asset(
        api_client,
        gl,
        cost=distinctive_cost,
        purchase_date="2025-01-01",
    )
    asset_id = asset["id"]

    # The default test client uses the default tenant. Verify our asset appears.
    r = await api_client.get("/api/v1/reports/depreciation_schedule")
    assert r.status_code == 200, r.text
    body = r.json()
    ids = [a["asset_id"] for a in body["assets"]]
    assert asset_id in ids, f"Expected asset {asset_id} in schedule for default tenant"

    # A client with a different tenant env var should not see this asset.
    alt_tenant_id = str(uuid.uuid4())
    original_env = os.environ.get("SAEBOOKS_DEV_TENANT_ID")
    try:
        os.environ["SAEBOOKS_DEV_TENANT_ID"] = alt_tenant_id
        # Resolve tenant at call time — need a fresh client to pick up env change.
        token = current_token()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        ) as alt_client:
            r2 = await alt_client.get("/api/v1/reports/depreciation_schedule")
        # Alt tenant may error (no company) or return empty — either is isolation.
        if r2.status_code == 200:
            alt_ids = [a["asset_id"] for a in r2.json()["assets"]]
            assert asset_id not in alt_ids
        else:
            assert r2.status_code in (404, 500)
    finally:
        if original_env is None:
            os.environ.pop("SAEBOOKS_DEV_TENANT_ID", None)
        else:
            os.environ["SAEBOOKS_DEV_TENANT_ID"] = original_env
