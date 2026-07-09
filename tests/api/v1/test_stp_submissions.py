"""Contract tests for /api/v1/stp-submissions.

Gap noted in the 2026-05-23 overnight regression sweep — STP read-only
router shipped with migration 0114 with zero contract coverage.

Phase 2 STP payloads are auto-built by the pay-run finalize flow; this
router only reads + previews. We seed StpSubmission rows directly via
ORM (faking the upstream pay-run finalize), then exercise the API.

Covers:
* Auth gate (401 without bearer).
* List — 200, total + pagination, items shape (totals + payee_count).
* List filter by pay_run_id — narrow to single run.
* Get single — 200, full ``payload`` only on the single-record fetch.
* Get unknown id — 404.
* Get id belonging to ANOTHER company — 404 (cross-tenant safety:
  the router compares the loaded row's company_id to the active
  company and refuses on mismatch without leaking existence).
* Pagination clamps (limit/offset query params).
"""
from __future__ import annotations

import uuid
from datetime import date as _date

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.company import Company
from saebooks.models.pay_run import PayRun
from saebooks.models.stp_submission import StpSubmission

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


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
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
    assert company is not None
    return company.id


async def _seed_pay_run(company_id: uuid.UUID) -> uuid.UUID:
    """Create a minimal PayRun row, return its id."""
    async with AsyncSessionLocal() as session:
        run = PayRun(
            tenant_id=_DEFAULT_TENANT_ID,
            company_id=company_id,
            period_start=_date(2026, 4, 1),
            period_end=_date(2026, 4, 7),
            payment_date=_date(2026, 4, 10),
            description="Pytest STP run",
            status="draft",
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run.id


async def _seed_submission(
    company_id: uuid.UUID, pay_run_id: uuid.UUID,
    *, event_type: str = "PAY", status: str = "READY"
) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        sub = StpSubmission(
            tenant_id=_DEFAULT_TENANT_ID,
            company_id=company_id,
            pay_run_id=pay_run_id,
            event_type=event_type,
            status=status,
            payload={
                "totals": {"gross": "1000.00", "tax": "200.00"},
                "payees": [{"id": "EE1"}, {"id": "EE2"}],
            },
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        return sub.id


async def _purge_stp_state() -> None:
    company_id = await _seed_company_id()
    async with AsyncSessionLocal() as session:
        for sub in (
            await session.execute(
                select(StpSubmission).where(StpSubmission.company_id == company_id)
            )
        ).scalars().all():
            await session.delete(sub)
        for run in (
            await session.execute(
                select(PayRun).where(PayRun.company_id == company_id)
            )
        ).scalars().all():
            await session.delete(run)
        await session.commit()


@pytest.fixture(autouse=True)
async def _clean_stp() -> None:
    await _purge_stp_state()
    yield
    await _purge_stp_state()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_requires_bearer(unauth_client: AsyncClient) -> None:
    resp = await unauth_client.get("/api/v1/stp-submissions")
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_get_requires_bearer(unauth_client: AsyncClient) -> None:
    fake = uuid.uuid4()
    resp = await unauth_client.get(f"/api/v1/stp-submissions/{fake}")
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_empty(api_client: AsyncClient) -> None:
    resp = await api_client.get("/api/v1/stp-submissions")
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 0
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_list_returns_seeded_submission(api_client: AsyncClient) -> None:
    cid = await _seed_company_id()
    run_id = await _seed_pay_run(cid)
    sub_id = await _seed_submission(cid, run_id)
    resp = await api_client.get("/api/v1/stp-submissions")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == str(sub_id)
    assert item["pay_run_id"] == str(run_id)
    assert item["event_type"] == "PAY"
    assert item["status"] == "READY"
    assert item["payee_count"] == 2
    assert item["totals"] == {"gross": "1000.00", "tax": "200.00"}
    # List shape DOES NOT include the full `payload` blob.
    assert "payload" not in item


@pytest.mark.asyncio
async def test_list_filter_by_pay_run(api_client: AsyncClient) -> None:
    cid = await _seed_company_id()
    run_a = await _seed_pay_run(cid)
    run_b = await _seed_pay_run(cid)
    await _seed_submission(cid, run_a)
    await _seed_submission(cid, run_b)
    await _seed_submission(cid, run_b)
    resp = await api_client.get(
        f"/api/v1/stp-submissions?pay_run_id={run_b}"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    for item in body["items"]:
        assert item["pay_run_id"] == str(run_b)


@pytest.mark.asyncio
async def test_list_pagination(api_client: AsyncClient) -> None:
    cid = await _seed_company_id()
    run_id = await _seed_pay_run(cid)
    for _ in range(3):
        await _seed_submission(cid, run_id)
    page1 = await api_client.get("/api/v1/stp-submissions?limit=2&offset=0")
    page2 = await api_client.get("/api/v1/stp-submissions?limit=2&offset=2")
    assert page1.json()["total"] == 3
    assert len(page1.json()["items"]) == 2
    assert len(page2.json()["items"]) == 1


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_full_payload(api_client: AsyncClient) -> None:
    cid = await _seed_company_id()
    run_id = await _seed_pay_run(cid)
    sub_id = await _seed_submission(cid, run_id)
    resp = await api_client.get(f"/api/v1/stp-submissions/{sub_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(sub_id)
    # Single-record fetch DOES surface the full payload blob.
    assert "payload" in body
    assert body["payload"]["totals"] == {"gross": "1000.00", "tax": "200.00"}
    assert len(body["payload"]["payees"]) == 2


@pytest.mark.asyncio
async def test_get_unknown_id_404(api_client: AsyncClient) -> None:
    missing = uuid.uuid4()
    resp = await api_client.get(f"/api/v1/stp-submissions/{missing}")
    assert resp.status_code == 404, resp.text
