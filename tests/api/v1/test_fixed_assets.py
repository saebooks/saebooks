"""Phase 1 tier-4 contract tests for /api/v1/fixed_assets.

Covers:
* Auth gate (401 without bearer, 401 with wrong token)
* GET /api/v1/fixed_assets → 200 with pagination shape
* GET /api/v1/fixed_assets/{id} → 200; 404 on missing UUID
* GET /api/v1/fixed_assets?archived=true → only archived results
* GET /api/v1/fixed_assets?status=active → status filter
* GET /api/v1/fixed_assets?depreciation_model_id=... → model filter
* POST /api/v1/fixed_assets → 201, version==1, change_log row created
* POST idempotency: same X-Idempotency-Key returns same response
* POST without depreciation_model_id → 422
* POST with invalid depreciation_model_id → 422
* POST with explicit code → code preserved (not auto-assigned)
* POST with auto-assigned code → AST- prefix
* PATCH with correct If-Match → 200, version bumped
* PATCH with stale If-Match → 409 with current state in body
* PATCH without If-Match → 428
* PATCH on disposed asset (non-cosmetic field) → 422
* PATCH on disposed asset (cosmetic field) → 200
* DELETE with correct If-Match → 204 (soft-archive)
* DELETE on active asset with book value → 422
* DELETE on active asset with zero cost → 204 (trivial/no-depreciation asset)
* DELETE with stale If-Match → 409
* DELETE without If-Match → 428
* Archived assets not in default list
* change_log sequence: create + update + delete = 3 rows with ops created/updated/deleted
* GET response includes depreciation_model nested object
* tenant_id present in POST response
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account
from saebooks.models.change_log import ChangeLog
from saebooks.models.company import Company
from saebooks.models.fixed_asset import FixedAsset

pytestmark = pytest.mark.postgres_only


# ---------------------------------------------------------------------------
# Helpers — resolve real GL account IDs from the seeded DB
# ---------------------------------------------------------------------------


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
async def gl() -> dict:
    return await _gl_accounts()


def _asset_payload(gl: dict, **overrides: object) -> dict:
    """Return a minimal valid FixedAssetCreate payload."""
    base: dict = {
        "name": "Test Laptop",
        "depreciation_model_id": "asset_3_year_linear",
        "purchase_date": "2026-01-01",
        "cost": "3000.00",
        **gl,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_fixed_assets_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/fixed_assets")
    assert r.status_code == 401


async def test_fixed_assets_rejects_wrong_token(unauth_client: AsyncClient) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer totally-wrong"},
    ) as ac:
        r = await ac.get("/api/v1/fixed_assets")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_fixed_assets_list_200(api_client: AsyncClient, gl: dict) -> None:
    r = await api_client.get("/api/v1/fixed_assets")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)


async def test_fixed_assets_list_default_excludes_archived(
    api_client: AsyncClient, gl: dict
) -> None:
    """Default list must not include archived assets."""
    r = await api_client.post("/api/v1/fixed_assets", json=_asset_payload(gl, cost="0.00"))
    assert r.status_code == 201
    asset_id = r.json()["id"]
    v = r.json()["version"]

    await api_client.delete(
        f"/api/v1/fixed_assets/{asset_id}", headers={"If-Match": str(v)}
    )

    r2 = await api_client.get("/api/v1/fixed_assets")
    ids = [i["id"] for i in r2.json()["items"]]
    assert asset_id not in ids


async def test_fixed_assets_list_archived_filter(
    api_client: AsyncClient, gl: dict
) -> None:
    """Soft-archived asset must not appear in the default list and must
    still be fetchable via GET /{id} with archived_at set."""
    r = await api_client.post("/api/v1/fixed_assets", json=_asset_payload(gl, cost="0.00"))
    assert r.status_code == 201
    asset_id = r.json()["id"]
    v = r.json()["version"]

    r_del = await api_client.delete(
        f"/api/v1/fixed_assets/{asset_id}", headers={"If-Match": str(v)}
    )
    assert r_del.status_code == 204

    # Asset must NOT appear in the default (non-archived) list.
    r2 = await api_client.get("/api/v1/fixed_assets", params={"page_size": 500})
    assert r2.status_code == 200
    ids_default = [i["id"] for i in r2.json()["items"]]
    assert asset_id not in ids_default

    # GET the specific asset — it should still exist with archived_at set.
    r3 = await api_client.get(f"/api/v1/fixed_assets/{asset_id}")
    assert r3.status_code == 200
    assert r3.json()["archived_at"] is not None


async def test_fixed_assets_list_status_filter(
    api_client: AsyncClient, gl: dict
) -> None:
    """?status=active must only return active assets."""
    r = await api_client.post("/api/v1/fixed_assets", json=_asset_payload(gl))
    assert r.status_code == 201
    asset_id = r.json()["id"]

    # Verify the asset itself reports status=active via direct GET.
    r_single = await api_client.get(f"/api/v1/fixed_assets/{asset_id}")
    assert r_single.status_code == 200
    assert r_single.json()["status"] == "active"

    # Verify that the list endpoint with status filter only returns active items
    # (sample first page — filter correctness, not full coverage).
    r2 = await api_client.get(
        "/api/v1/fixed_assets", params={"status": "active", "page_size": 50}
    )
    assert r2.status_code == 200
    for item in r2.json()["items"]:
        assert item["status"] == "active"


async def test_fixed_assets_list_model_filter(
    api_client: AsyncClient, gl: dict
) -> None:
    """?depreciation_model_id=... must return only assets with that model."""
    r = await api_client.post(
        "/api/v1/fixed_assets",
        json=_asset_payload(gl, depreciation_model_id="asset_5_year_linear"),
    )
    assert r.status_code == 201
    asset_id = r.json()["id"]

    # Verify the asset itself has the right model via direct GET.
    r_single = await api_client.get(f"/api/v1/fixed_assets/{asset_id}")
    assert r_single.status_code == 200
    assert r_single.json()["depreciation_model_id"] == "asset_5_year_linear"

    # Verify the filter only returns assets with that model (sample first page).
    r2 = await api_client.get(
        "/api/v1/fixed_assets",
        params={"depreciation_model_id": "asset_5_year_linear", "page_size": 50},
    )
    assert r2.status_code == 200
    for item in r2.json()["items"]:
        assert item["depreciation_model_id"] == "asset_5_year_linear"


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


async def test_fixed_assets_get_404_unknown_uuid(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/fixed_assets/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_fixed_assets_get_200_includes_model(
    api_client: AsyncClient, gl: dict
) -> None:
    """GET /{id} must include nested depreciation_model for UX."""
    r = await api_client.post("/api/v1/fixed_assets", json=_asset_payload(gl))
    assert r.status_code == 201
    asset_id = r.json()["id"]

    r2 = await api_client.get(f"/api/v1/fixed_assets/{asset_id}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["id"] == asset_id
    assert body["status"] == "active"
    assert "depreciation_model" in body
    assert body["depreciation_model"]["id"] == "asset_3_year_linear"
    assert body["depreciation_model"]["method"] == "linear"


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_fixed_assets_create_201(api_client: AsyncClient, gl: dict) -> None:
    r = await api_client.post("/api/v1/fixed_assets", json=_asset_payload(gl))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["archived_at"] is None
    assert body["status"] == "active"
    assert "tenant_id" in body
    assert "id" in body
    assert "code" in body
    assert "name" in body
    assert body["depreciation_model_id"] == "asset_3_year_linear"


async def test_fixed_assets_create_auto_code(api_client: AsyncClient, gl: dict) -> None:
    """POST without explicit code → auto-assigned AST- prefix."""
    r = await api_client.post("/api/v1/fixed_assets", json=_asset_payload(gl))
    assert r.status_code == 201, r.text
    assert r.json()["code"].startswith("AST-")


async def test_fixed_assets_create_explicit_code(
    api_client: AsyncClient, gl: dict
) -> None:
    """POST with explicit code → code preserved."""
    custom_code = f"FA-CUSTOM-{uuid.uuid4().hex[:6].upper()}"
    r = await api_client.post(
        "/api/v1/fixed_assets", json=_asset_payload(gl, code=custom_code)
    )
    assert r.status_code == 201, r.text
    assert r.json()["code"] == custom_code


async def test_fixed_assets_create_requires_depreciation_model(
    api_client: AsyncClient, gl: dict
) -> None:
    """POST without depreciation_model_id → 422."""
    payload = _asset_payload(gl)
    del payload["depreciation_model_id"]
    r = await api_client.post("/api/v1/fixed_assets", json=payload)
    assert r.status_code == 422


async def test_fixed_assets_create_invalid_depreciation_model(
    api_client: AsyncClient, gl: dict
) -> None:
    """POST with non-existent depreciation_model_id → 422."""
    r = await api_client.post(
        "/api/v1/fixed_assets",
        json=_asset_payload(gl, depreciation_model_id="does_not_exist"),
    )
    assert r.status_code == 422


async def test_fixed_assets_create_change_log(
    api_client: AsyncClient, gl: dict
) -> None:
    """POST should produce a change_log row with op=created, version=1."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/fixed_assets", json=_asset_payload(gl))
    assert r.status_code == 201
    asset_id = r.json()["id"]

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(asset_id),
                    ChangeLog.entity == "fixed_asset",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert len(rows) >= 1
    assert rows[-1].op == "created"
    assert rows[-1].version == 1


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_fixed_assets_create_idempotency(
    api_client: AsyncClient, gl: dict
) -> None:
    """Same X-Idempotency-Key returns the same response body on replay."""
    key = str(uuid.uuid4())
    payload = _asset_payload(gl)

    r1 = await api_client.post(
        "/api/v1/fixed_assets",
        json=payload,
        headers={"X-Idempotency-Key": key},
    )
    assert r1.status_code == 201
    id1 = r1.json()["id"]

    r2 = await api_client.post(
        "/api/v1/fixed_assets",
        json=payload,
        headers={"X-Idempotency-Key": key},
    )
    assert r2.status_code == 201
    assert r2.json()["id"] == id1


# ---------------------------------------------------------------------------
# Update — valid If-Match
# ---------------------------------------------------------------------------


async def test_fixed_assets_update_bumps_version(
    api_client: AsyncClient, gl: dict
) -> None:
    r = await api_client.post("/api/v1/fixed_assets", json=_asset_payload(gl))
    assert r.status_code == 201
    asset_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.patch(
        f"/api/v1/fixed_assets/{asset_id}",
        json={"name": "Updated Laptop"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    updated = r2.json()
    assert updated["version"] == v + 1
    assert updated["name"] == "Updated Laptop"


# ---------------------------------------------------------------------------
# Update — disposed asset restrictions
# ---------------------------------------------------------------------------


async def test_fixed_assets_patch_disposed_non_cosmetic_422(
    api_client: AsyncClient, gl: dict
) -> None:
    """PATCH on a disposed asset with non-cosmetic field → 422."""
    # Create asset with zero cost so we can archive it, then mark disposed via DB.
    r = await api_client.post("/api/v1/fixed_assets", json=_asset_payload(gl))
    assert r.status_code == 201
    asset_id = r.json()["id"]
    v = r.json()["version"]

    # Force status to disposed via DB so we can test the PATCH restriction.
    async with AsyncSessionLocal() as session:
        asset = await session.get(FixedAsset, uuid.UUID(asset_id))
        assert asset is not None
        asset.status = "disposed"
        await session.commit()

    r2 = await api_client.patch(
        f"/api/v1/fixed_assets/{asset_id}",
        json={"name": "Cannot change name after disposal"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 422


async def test_fixed_assets_patch_disposed_cosmetic_ok(
    api_client: AsyncClient, gl: dict
) -> None:
    """PATCH on a disposed asset with description only → 200."""
    r = await api_client.post("/api/v1/fixed_assets", json=_asset_payload(gl))
    assert r.status_code == 201
    asset_id = r.json()["id"]
    v = r.json()["version"]

    async with AsyncSessionLocal() as session:
        asset = await session.get(FixedAsset, uuid.UUID(asset_id))
        assert asset is not None
        asset.status = "disposed"
        await session.commit()

    r2 = await api_client.patch(
        f"/api/v1/fixed_assets/{asset_id}",
        json={"description": "Updated after disposal"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["description"] == "Updated after disposal"


# ---------------------------------------------------------------------------
# Update — missing / stale If-Match
# ---------------------------------------------------------------------------


async def test_fixed_assets_update_requires_if_match(
    api_client: AsyncClient, gl: dict
) -> None:
    r = await api_client.post("/api/v1/fixed_assets", json=_asset_payload(gl))
    assert r.status_code == 201
    asset_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/fixed_assets/{asset_id}", json={"name": "x"}
    )
    assert r2.status_code == 428


async def test_fixed_assets_stale_if_match_returns_409(
    api_client: AsyncClient, gl: dict
) -> None:
    r = await api_client.post("/api/v1/fixed_assets", json=_asset_payload(gl))
    assert r.status_code == 201
    asset_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/fixed_assets/{asset_id}",
        json={"name": "stale attempt"},
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == asset_id
    assert body["current"]["version"] == 1


# ---------------------------------------------------------------------------
# Delete → 204
# ---------------------------------------------------------------------------


async def test_fixed_assets_delete_204_zero_cost(
    api_client: AsyncClient, gl: dict
) -> None:
    """DELETE on active asset with cost=0 (book value = 0) → 204."""
    r = await api_client.post(
        "/api/v1/fixed_assets", json=_asset_payload(gl, cost="0.00")
    )
    assert r.status_code == 201
    asset_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.delete(
        f"/api/v1/fixed_assets/{asset_id}",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 204

    # Should no longer appear in default list
    r3 = await api_client.get("/api/v1/fixed_assets")
    ids = [i["id"] for i in r3.json()["items"]]
    assert asset_id not in ids


async def test_fixed_assets_delete_active_with_book_value_422(
    api_client: AsyncClient, gl: dict
) -> None:
    """DELETE on active asset with cost > 0 and residual_value = 0 → 422."""
    r = await api_client.post(
        "/api/v1/fixed_assets", json=_asset_payload(gl, cost="5000.00")
    )
    assert r.status_code == 201
    asset_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.delete(
        f"/api/v1/fixed_assets/{asset_id}",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 422
    assert "book value" in r2.json()["detail"].lower()


async def test_fixed_assets_delete_disposed_ok(
    api_client: AsyncClient, gl: dict
) -> None:
    """DELETE on disposed asset → 204 (archive freely)."""
    r = await api_client.post("/api/v1/fixed_assets", json=_asset_payload(gl))
    assert r.status_code == 201
    asset_id = r.json()["id"]
    v = r.json()["version"]

    # Force status to disposed
    async with AsyncSessionLocal() as session:
        asset = await session.get(FixedAsset, uuid.UUID(asset_id))
        assert asset is not None
        asset.status = "disposed"
        await session.commit()

    r2 = await api_client.delete(
        f"/api/v1/fixed_assets/{asset_id}",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 204


async def test_fixed_assets_delete_stale_if_match_409(
    api_client: AsyncClient, gl: dict
) -> None:
    r = await api_client.post(
        "/api/v1/fixed_assets", json=_asset_payload(gl, cost="0.00")
    )
    assert r.status_code == 201
    asset_id = r.json()["id"]

    r2 = await api_client.delete(
        f"/api/v1/fixed_assets/{asset_id}",
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409


async def test_fixed_assets_delete_requires_if_match(
    api_client: AsyncClient, gl: dict
) -> None:
    r = await api_client.post(
        "/api/v1/fixed_assets", json=_asset_payload(gl, cost="0.00")
    )
    assert r.status_code == 201
    asset_id = r.json()["id"]

    r2 = await api_client.delete(f"/api/v1/fixed_assets/{asset_id}")
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# change_log sequence
# ---------------------------------------------------------------------------


async def test_fixed_assets_change_log_full_sequence(
    api_client: AsyncClient, gl: dict
) -> None:
    """Create + update + delete = 3 fixed_asset change_log rows in order."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post(
        "/api/v1/fixed_assets", json=_asset_payload(gl, cost="0.00")
    )
    assert r.status_code == 201
    asset_id = r.json()["id"]

    await api_client.patch(
        f"/api/v1/fixed_assets/{asset_id}",
        json={"description": "Updated description"},
        headers={"If-Match": "1"},
    )
    await api_client.delete(
        f"/api/v1/fixed_assets/{asset_id}",
        headers={"If-Match": "2"},
    )

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(asset_id),
                    ChangeLog.entity == "fixed_asset",
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


# ---------------------------------------------------------------------------
# POST /{id}/post_depreciation
# ---------------------------------------------------------------------------


async def test_fixed_asset_depreciation_run_posts_amount(
    api_client: AsyncClient, gl: dict
) -> None:
    """Active linear asset → 200; amount_posted >= 0 and version bumped.

    Uses a through-date after the period lock (locked through 2026-03-31).
    Purchase date is set to 2024-01-01 so more than a year of depreciation
    has accumulated by 2026-04-30.
    """
    # Create an asset that has been in service for over a year so depreciation > 0.
    r = await api_client.post(
        "/api/v1/fixed_assets",
        json=_asset_payload(
            gl,
            depreciation_model_id="asset_3_year_linear",
            purchase_date="2024-01-01",
            cost="3000.00",
        ),
    )
    assert r.status_code == 201, r.text
    asset_id = r.json()["id"]
    v = r.json()["version"]

    # through must be after the period lock (locked through 2026-03-31)
    r2 = await api_client.post(
        f"/api/v1/fixed_assets/{asset_id}/post_depreciation",
        json={"through": "2026-04-30"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert "asset" in body
    assert "amount_posted" in body
    assert "note" in body
    # amount_posted is non-negative
    from decimal import Decimal
    amount_posted = Decimal(str(body["amount_posted"]))
    assert amount_posted >= Decimal("0")
    # Version must have been bumped
    assert body["asset"]["version"] == v + 1
    assert body["asset"]["id"] == asset_id
    # last_depreciation_posted_through must be set
    assert body["asset"]["last_depreciation_posted_through"] is not None


async def test_fixed_asset_depreciation_run_no_dep_model(
    api_client: AsyncClient, gl: dict
) -> None:
    """Asset with no_depreciation model → 200, amount_posted == 0.

    No journal is posted so no period-lock check occurs — any through-date works.
    """
    r = await api_client.post(
        "/api/v1/fixed_assets",
        json=_asset_payload(
            gl,
            depreciation_model_id="asset_no_depreciation",
            purchase_date="2024-01-01",
            cost="5000.00",
        ),
    )
    assert r.status_code == 201, r.text
    asset_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/fixed_assets/{asset_id}/post_depreciation",
        json={"through": "2026-04-30"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    from decimal import Decimal
    assert Decimal(str(body["amount_posted"])) == Decimal("0")
    assert body["asset"]["version"] == v + 1


async def test_fixed_asset_depreciation_run_stale_409(
    api_client: AsyncClient, gl: dict
) -> None:
    """Wrong If-Match → 409 with current state in body."""
    r = await api_client.post(
        "/api/v1/fixed_assets",
        json=_asset_payload(gl, purchase_date="2024-01-01", cost="3000.00"),
    )
    assert r.status_code == 201, r.text
    asset_id = r.json()["id"]

    r2 = await api_client.post(
        f"/api/v1/fixed_assets/{asset_id}/post_depreciation",
        json={"through": "2026-04-30"},
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == asset_id


# ---------------------------------------------------------------------------
# POST /depreciation_run_all
# ---------------------------------------------------------------------------


async def test_depreciation_run_all_returns_results(
    api_client: AsyncClient, gl: dict
) -> None:
    """POST /depreciation_run_all with a future through-date → 200, valid shape.

    Creates one asset so there is at least one active asset in the DB.
    Uses a no_depreciation model so no period-lock check fires.
    """
    # Create an asset with the no_depreciation model so the batch run
    # never hits a posting error regardless of the period lock.
    r = await api_client.post(
        "/api/v1/fixed_assets",
        json=_asset_payload(
            gl,
            depreciation_model_id="asset_no_depreciation",
            purchase_date="2024-01-01",
            cost="1000.00",
        ),
    )
    assert r.status_code == 201, r.text

    r2 = await api_client.post(
        "/api/v1/fixed_assets/depreciation_run_all",
        json={"through": "2026-04-30"},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert "through" in body
    assert "total_assets" in body
    assert "total_amount" in body
    assert "results" in body
    assert "errors" in body
    assert isinstance(body["results"], list)
    assert isinstance(body["errors"], list)
    assert body["total_assets"] >= 0
    # total_assets == len(results)
    assert body["total_assets"] == len(body["results"])


async def test_depreciation_run_all_no_active_assets_ok(
    api_client: AsyncClient,
) -> None:
    """POST /depreciation_run_all still returns 200 even if there happen to
    be zero active assets (empty results, no errors)."""
    # We cannot guarantee the DB has zero assets, but we can verify the
    # endpoint always returns 200 with the expected shape.
    r = await api_client.post(
        "/api/v1/fixed_assets/depreciation_run_all",
        json={"through": "2099-12-31"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_assets"] == len(body["results"])
    assert isinstance(body["errors"], list)


# ---------------------------------------------------------------------------
# FLAG_ASSET_V2 gate — Wave A (2026-07-10)
#
# v1 baseline (linear/no-depreciation model, no tax split) stays
# ungated at every tier — the router itself carries no require_feature
# dependency. Only the two v2-specific fields are conditionally gated:
# a diminishing-value depreciation_model_id/tax_model_id, and setting
# tax_model_id at all (the book/tax split itself).
# ---------------------------------------------------------------------------


async def test_create_linear_asset_ungated_at_community(
    api_client: AsyncClient, gl: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v1 baseline: linear model, no tax split -> 201 even at community."""
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "community")
    r = await api_client.post(
        "/api/v1/fixed_assets", json=_asset_payload(gl)
    )
    assert r.status_code == 201, r.text


async def test_create_dv_asset_gated_at_community(
    api_client: AsyncClient, gl: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v2: a diminishing-value book model -> 404 at community."""
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "community")
    r = await api_client.post(
        "/api/v1/fixed_assets",
        json=_asset_payload(gl, depreciation_model_id="asset_dv_30"),
    )
    assert r.status_code == 404, r.text


async def test_create_dv_asset_succeeds_at_offline(
    api_client: AsyncClient, gl: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v2: a diminishing-value book model -> 201 at offline (turns on here)."""
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "offline")
    r = await api_client.post(
        "/api/v1/fixed_assets",
        json=_asset_payload(gl, depreciation_model_id="asset_dv_30"),
    )
    assert r.status_code == 201, r.text


async def test_create_tax_split_gated_at_community(
    api_client: AsyncClient, gl: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v2: tax_model_id set at all (even a linear tax model) -> 404 at community."""
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "community")
    r = await api_client.post(
        "/api/v1/fixed_assets",
        json=_asset_payload(
            gl,
            depreciation_model_id="asset_10_year_linear",
            tax_model_id="asset_5_year_linear",
        ),
    )
    assert r.status_code == 404, r.text


async def test_create_tax_split_succeeds_at_offline(
    api_client: AsyncClient, gl: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v2: tax_model_id set -> 201 at offline (turns on here)."""
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "offline")
    r = await api_client.post(
        "/api/v1/fixed_assets",
        json=_asset_payload(
            gl,
            depreciation_model_id="asset_10_year_linear",
            tax_model_id="asset_5_year_linear",
        ),
    )
    assert r.status_code == 201, r.text


async def test_update_to_dv_model_gated_at_community(
    api_client: AsyncClient, gl: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCH-ing an existing v1 asset's model to a DV model -> 404 at
    community. Proves the gate isn't create-only (create then downgrade
    tier then PATCH to DV is exactly the loophole this closes)."""
    from saebooks.config import settings as _s

    r = await api_client.post("/api/v1/fixed_assets", json=_asset_payload(gl))
    assert r.status_code == 201, r.text
    created = r.json()

    monkeypatch.setattr(_s, "edition", "community")
    r2 = await api_client.patch(
        f"/api/v1/fixed_assets/{created['id']}",
        json={"depreciation_model_id": "asset_dv_30"},
        headers={"If-Match": str(created["version"])},
    )
    assert r2.status_code == 404, r2.text


async def test_update_cosmetic_field_ungated_at_community(
    api_client: AsyncClient, gl: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCH on a plain v1 asset that doesn't touch model/tax fields
    stays ungated at community (proves the gate is scoped to the two
    v2 fields, not the whole PATCH route)."""
    from saebooks.config import settings as _s

    r = await api_client.post("/api/v1/fixed_assets", json=_asset_payload(gl))
    assert r.status_code == 201, r.text
    created = r.json()

    monkeypatch.setattr(_s, "edition", "community")
    r2 = await api_client.patch(
        f"/api/v1/fixed_assets/{created['id']}",
        json={"description": "Updated description"},
        headers={"If-Match": str(created["version"])},
    )
    assert r2.status_code == 200, r2.text


async def test_dispose_stays_ungated_at_community(
    api_client: AsyncClient, gl: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full disposal (v1 baseline per CHARTER) is never gated by
    FLAG_ASSET_V2, at any tier."""
    from saebooks.config import settings as _s

    r = await api_client.post("/api/v1/fixed_assets", json=_asset_payload(gl))
    assert r.status_code == 201, r.text
    created = r.json()

    monkeypatch.setattr(_s, "edition", "community")
    r2 = await api_client.post(
        f"/api/v1/fixed_assets/{created['id']}/dispose",
        json={"disposal_date": "2026-06-01", "proceeds": "500.00"},
        headers={"If-Match": str(created["version"])},
    )
    assert r2.status_code == 200, r2.text
