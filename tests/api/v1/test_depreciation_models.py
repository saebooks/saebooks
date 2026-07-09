"""Contract tests for GET /api/v1/depreciation_models.

Covers:
* Auth gate (401 without bearer)
* List returns 200 with ``items`` array and ``total`` >= 1 (seeded data)
* Each item has ``id`` (str) and ``method`` (str) fields
* ``limit``/``offset`` params work — limit=2 returns 2 items when total >= 2
* Unknown query params are ignored gracefully (FastAPI does not reject them)
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from saebooks.api.v1.auth import current_token
from saebooks.main import app

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


@pytest.fixture
async def unauth_client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_depreciation_models_requires_bearer(
    unauth_client: AsyncClient,
) -> None:
    r = await unauth_client.get("/api/v1/depreciation_models")
    assert r.status_code == 401


async def test_depreciation_models_rejects_wrong_token(
    unauth_client: AsyncClient,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer totally-wrong"},
    ) as ac:
        r = await ac.get("/api/v1/depreciation_models")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_depreciation_models_list_200(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/depreciation_models")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert body["total"] >= 1
    assert isinstance(body["items"], list)
    assert len(body["items"]) >= 1


async def test_depreciation_models_list_item_shape(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/depreciation_models")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) >= 1
    first = items[0]
    # id is a slug string, not a UUID
    assert isinstance(first["id"], str)
    assert len(first["id"]) > 0
    assert isinstance(first["method"], str)
    # numeric fields present
    assert "method_number" in first
    assert "method_period" in first
    # optional fields present in schema (may be null)
    assert "method_progress_factor" in first
    assert "rate_pct" in first
    assert "created_at" in first


async def test_depreciation_models_no_uuids_as_ids(api_client: AsyncClient) -> None:
    """All model ids should be human-readable slugs, not UUID strings."""
    r = await api_client.get("/api/v1/depreciation_models")
    assert r.status_code == 200
    for item in r.json()["items"]:
        # Slugs contain underscores; real UUIDs contain hyphens in 8-4-4-4-12 pattern
        assert "-" not in item["id"] or "_" in item["id"]


async def test_depreciation_models_limit_param(api_client: AsyncClient) -> None:
    """limit=2 should return at most 2 items when total >= 2."""
    # First confirm we have enough rows
    r_all = await api_client.get("/api/v1/depreciation_models")
    total = r_all.json()["total"]
    if total < 2:
        pytest.skip("Seeded DB has fewer than 2 depreciation models — cannot test limit")

    r = await api_client.get("/api/v1/depreciation_models", params={"limit": 2})
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 2
    assert body["total"] == total
    assert body["limit"] == 2


async def test_depreciation_models_offset_param(api_client: AsyncClient) -> None:
    """offset=1 should skip the first item."""
    r_all = await api_client.get("/api/v1/depreciation_models")
    all_items = r_all.json()["items"]
    total = r_all.json()["total"]
    if total < 2:
        pytest.skip("Seeded DB has fewer than 2 depreciation models — cannot test offset")

    r = await api_client.get("/api/v1/depreciation_models", params={"offset": 1})
    assert r.status_code == 200
    offset_items = r.json()["items"]
    # The first item with offset=1 should equal the second item with no offset
    assert offset_items[0]["id"] == all_items[1]["id"]


async def test_depreciation_models_unknown_params_ignored(
    api_client: AsyncClient,
) -> None:
    """FastAPI ignores unknown query parameters — should not raise 422."""
    r = await api_client.get(
        "/api/v1/depreciation_models",
        params={"totally_unknown_filter": "foo", "another_fake_sort": "bar"},
    )
    assert r.status_code == 200


async def test_depreciation_models_contains_no_depreciation(
    api_client: AsyncClient,
) -> None:
    """The seed must include the 'asset_no_depreciation' row."""
    r = await api_client.get("/api/v1/depreciation_models", params={"limit": 200})
    assert r.status_code == 200
    ids = [item["id"] for item in r.json()["items"]]
    assert "asset_no_depreciation" in ids
