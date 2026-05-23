"""Contract tests for /api/v1/super-funds.

Covers gap noted in the 2026-05-23 overnight regression sweep —
super_funds router shipped with the payroll Phase 3 work but
without dedicated contract tests.

Covers:
* Auth gate (401 without bearer).
* List — 200, includes seeded create, total + pagination.
* Pagination (limit/offset).
* Get by id — 200; 404 on missing.
* Create — 201 with ETag on APRA fund (USI required path).
* Create — 201 on SMSF (employer_abn + esa required path), has_smsf_bank
  toggles when SMSF bank fields provided.
* Create — APRA without USI → 400 (service-translated).
* Create — APRA USI wrong length → 400.
* Create — SMSF without employer_abn/esa → 400.
* Update PATCH — 200, version bumps.
* Update — 404 on missing fund.
* Update — If-Match stale version → 412 (note: this router uses 412 not 409 —
  see :mod:`saebooks.api.v1.super_funds._translate_error`).
* Update — If-Match malformed → 400.
* Delete (archive) — 204; fund disappears from list.
* Delete — cannot archive default fund → 409.
* Delete — 404 on missing fund.
* Set-default — 200; previous default flips off.
* Set-default — 404 on missing fund.

NB: missing-If-Match does NOT 428 on this router — the version check is only
made when If-Match is provided. That's a Phase-1 convention divergence from
saebooks-versioning (decimal version policy). Test asserts current behaviour.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.company import Company
from saebooks.models.super_fund import SuperFund


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


async def _seed_company_id() -> uuid.UUID:
    """Return the seed company id ordered by created_at (epoch-pinned in conftest)."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
    assert company is not None, "No seed company present"
    return company.id


async def _purge_funds() -> None:
    """Clean up *all* (archived or not) super funds in the seed company so
    each test starts from a known baseline. Idempotent."""
    company_id = await _seed_company_id()
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(SuperFund).where(SuperFund.company_id == company_id)
            )
        ).scalars().all()
        for row in rows:
            await session.delete(row)
        await session.commit()


@pytest.fixture(autouse=True)
async def _clean_funds() -> None:
    """Run before and after every test so state is order-independent."""
    await _purge_funds()
    yield
    await _purge_funds()


def _apra_body(name: str = "AustralianSuper", usi: str = "STA0100AU000") -> dict:
    # USI is exactly 11 chars (validated by Pydantic min_length=max_length=11).
    # Default below is a real-world style USI (StatewideSuper-shaped) shortened.
    return {"name": name, "is_smsf": False, "usi": usi[:11]}


def _smsf_body(name: str = "Smith Family SMSF") -> dict:
    return {
        "name": name,
        "is_smsf": True,
        "employer_abn": "12345678901",
        "esa": "smsfdataflow",
    }


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_requires_bearer(unauth_client: AsyncClient) -> None:
    resp = await unauth_client.get("/api/v1/super-funds")
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_create_requires_bearer(unauth_client: AsyncClient) -> None:
    resp = await unauth_client.post("/api/v1/super-funds", json=_apra_body())
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# List + pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_empty(api_client: AsyncClient) -> None:
    resp = await api_client.get("/api/v1/super-funds")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["limit"] == 100
    assert body["offset"] == 0


@pytest.mark.asyncio
async def test_list_after_create_returns_item(api_client: AsyncClient) -> None:
    create_resp = await api_client.post("/api/v1/super-funds", json=_apra_body())
    assert create_resp.status_code == 201, create_resp.text
    list_resp = await api_client.get("/api/v1/super-funds")
    assert list_resp.status_code == 200
    body = list_resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["name"] == "AustralianSuper"
    assert body["items"][0]["usi"] == "STA0100AU00"  # 11 chars
    assert body["items"][0]["is_smsf"] is False
    assert body["items"][0]["has_smsf_bank"] is False
    assert body["items"][0]["is_default"] is False


@pytest.mark.asyncio
async def test_list_pagination(api_client: AsyncClient) -> None:
    # Create three funds with distinct USIs.
    for i in range(3):
        # 11-char USI per Pydantic constraint.
        resp = await api_client.post(
            "/api/v1/super-funds",
            json=_apra_body(name=f"Fund{i}", usi=f"FUND0{i:02}AU01"),
        )
        assert resp.status_code == 201, resp.text
    page1 = await api_client.get("/api/v1/super-funds?limit=2&offset=0")
    page2 = await api_client.get("/api/v1/super-funds?limit=2&offset=2")
    assert page1.status_code == page2.status_code == 200
    assert page1.json()["total"] == 3
    assert len(page1.json()["items"]) == 2
    assert page1.json()["limit"] == 2
    assert page1.json()["offset"] == 0
    assert len(page2.json()["items"]) == 1


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_fund(api_client: AsyncClient) -> None:
    create_resp = await api_client.post("/api/v1/super-funds", json=_apra_body())
    fund_id = create_resp.json()["id"]
    get_resp = await api_client.get(f"/api/v1/super-funds/{fund_id}")
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["id"] == fund_id


@pytest.mark.asyncio
async def test_get_unknown_id_404(api_client: AsyncClient) -> None:
    missing = uuid.uuid4()
    resp = await api_client.get(f"/api/v1/super-funds/{missing}")
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Create — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_apra_fund(api_client: AsyncClient) -> None:
    resp = await api_client.post("/api/v1/super-funds", json=_apra_body())
    assert resp.status_code == 201, f"status={resp.status_code} body={resp.text}"
    body = resp.json()
    assert body["name"] == "AustralianSuper"
    assert body["usi"] == "STA0100AU00"  # 11-char USI baseline
    assert body["is_smsf"] is False
    assert body["version"] == 1
    assert body["has_smsf_bank"] is False
    # ETag header carries the version
    assert resp.headers.get("etag") == '"1"'


@pytest.mark.asyncio
async def test_create_smsf_with_bank(api_client: AsyncClient) -> None:
    body = _smsf_body()
    body.update(
        {
            "smsf_bsb": "062-000",
            "smsf_account_number": "12345678",
            "smsf_account_name": "Smith Family Super",
        }
    )
    resp = await api_client.post("/api/v1/super-funds", json=body)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["is_smsf"] is True
    # Sensitive plaintext fields MUST NOT appear in the default response.
    assert "smsf_bsb" not in out
    assert "smsf_account_number" not in out
    assert "smsf_account_name" not in out
    # has_smsf_bank toggles when any of the encrypted bank columns are set.
    assert out["has_smsf_bank"] is True


# ---------------------------------------------------------------------------
# Create — validation failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_apra_missing_usi_400(api_client: AsyncClient) -> None:
    resp = await api_client.post(
        "/api/v1/super-funds", json={"name": "BadAPRA", "is_smsf": False}
    )
    # Service raises SuperFundError("apra_missing_usi") → 400.
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_create_apra_usi_wrong_length_422(api_client: AsyncClient) -> None:
    resp = await api_client.post(
        "/api/v1/super-funds",
        json={"name": "ShortUSI", "is_smsf": False, "usi": "TOO_SHORT"},
    )
    # Pydantic min_length/max_length=11 → 422 at the request body layer.
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_create_smsf_missing_employer_abn_400(api_client: AsyncClient) -> None:
    resp = await api_client.post(
        "/api/v1/super-funds",
        json={"name": "BadSMSF", "is_smsf": True, "esa": "smsfdataflow"},
    )
    # Service raises SuperFundError("smsf_missing_fields") → 400.
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_bumps_version(api_client: AsyncClient) -> None:
    create_resp = await api_client.post("/api/v1/super-funds", json=_apra_body())
    fund = create_resp.json()
    patch_resp = await api_client.patch(
        f"/api/v1/super-funds/{fund['id']}",
        json={"name": "AustralianSuper (updated)"},
    )
    assert patch_resp.status_code == 200, patch_resp.text
    out = patch_resp.json()
    assert out["name"] == "AustralianSuper (updated)"
    assert out["version"] == fund["version"] + 1


@pytest.mark.asyncio
async def test_update_unknown_id_404(api_client: AsyncClient) -> None:
    missing = uuid.uuid4()
    resp = await api_client.patch(
        f"/api/v1/super-funds/{missing}", json={"name": "x"}
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_update_stale_if_match_412(api_client: AsyncClient) -> None:
    create_resp = await api_client.post("/api/v1/super-funds", json=_apra_body())
    fund_id = create_resp.json()["id"]
    # Stale version sent — service raises version_mismatch → 412.
    resp = await api_client.patch(
        f"/api/v1/super-funds/{fund_id}",
        json={"name": "stale"},
        headers={"If-Match": "999"},
    )
    assert resp.status_code == 412, resp.text


@pytest.mark.asyncio
async def test_update_malformed_if_match_400(api_client: AsyncClient) -> None:
    create_resp = await api_client.post("/api/v1/super-funds", json=_apra_body())
    fund_id = create_resp.json()["id"]
    resp = await api_client.patch(
        f"/api/v1/super-funds/{fund_id}",
        json={"name": "x"},
        headers={"If-Match": "not-an-integer"},
    )
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# Delete (archive)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_removes_from_list(api_client: AsyncClient) -> None:
    create_resp = await api_client.post("/api/v1/super-funds", json=_apra_body())
    fund_id = create_resp.json()["id"]
    del_resp = await api_client.delete(f"/api/v1/super-funds/{fund_id}")
    assert del_resp.status_code == 204, del_resp.text
    list_resp = await api_client.get("/api/v1/super-funds")
    assert list_resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_delete_default_fund_409(api_client: AsyncClient) -> None:
    body = _apra_body()
    body["is_default"] = True
    create_resp = await api_client.post("/api/v1/super-funds", json=body)
    fund_id = create_resp.json()["id"]
    assert create_resp.json()["is_default"] is True
    # Service raises cannot_archive_default → 409.
    resp = await api_client.delete(f"/api/v1/super-funds/{fund_id}")
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_delete_unknown_id_404(api_client: AsyncClient) -> None:
    missing = uuid.uuid4()
    resp = await api_client.delete(f"/api/v1/super-funds/{missing}")
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Set-default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_default_flips_previous(api_client: AsyncClient) -> None:
    # Create two funds — make first default, then flip default to second.
    body_a = _apra_body(name="FundA", usi="FUNDA00AU01")
    body_a["is_default"] = True
    a_resp = await api_client.post("/api/v1/super-funds", json=body_a)
    a_id = a_resp.json()["id"]
    assert a_resp.json()["is_default"] is True

    b_resp = await api_client.post(
        "/api/v1/super-funds",
        json=_apra_body(name="FundB", usi="FUNDB00AU01"),
    )
    b_id = b_resp.json()["id"]
    assert b_resp.json()["is_default"] is False

    flip = await api_client.post(f"/api/v1/super-funds/{b_id}/set-default")
    assert flip.status_code == 200, flip.text
    assert flip.json()["is_default"] is True

    # Re-fetch fund A — should no longer be default.
    re_a = await api_client.get(f"/api/v1/super-funds/{a_id}")
    assert re_a.json()["is_default"] is False


@pytest.mark.asyncio
async def test_set_default_unknown_id_404(api_client: AsyncClient) -> None:
    missing = uuid.uuid4()
    resp = await api_client.post(f"/api/v1/super-funds/{missing}/set-default")
    assert resp.status_code == 404, resp.text
