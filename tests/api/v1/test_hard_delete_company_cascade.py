"""Company hard-delete cascade tests (gap ADMIN-DELETE-1).

* 409 + blocking_refs when txns exist
* 204 when clean
* audit_log written on success
* admin gate (no X-Admin → 403)
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from saebooks.api.v1.auth import current_token, resolve_tenant_id, DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.audit_log import AuditLog
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


async def _make_clean_company() -> uuid.UUID:
    """Create a fresh company with no related rows."""
    async with AsyncSessionLocal() as s:
        c = Company(
            tenant_id=DEFAULT_TENANT_ID,
            name=f"HardDeleteTestCo-{uuid.uuid4().hex[:8]}",
        )
        s.add(c)
        await s.commit()
        await s.refresh(c)
        return c.id


async def test_company_clean_hard_delete_204(api_client: AsyncClient) -> None:
    cid = await _make_clean_company()

    r = await api_client.delete(
        f"/api/v1/companies/{cid}?hard=true",
        headers={"X-Admin": "true"},
    )
    assert r.status_code == 204, r.text

    async with AsyncSessionLocal() as s:
        row = await s.get(Company, cid)
        assert row is None
        log = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.table_name == "companies",
                    AuditLog.row_id == str(cid),
                )
            )
        ).scalars().first()
        assert log is not None
        assert log.action == "hard_delete"


async def test_company_with_refs_returns_409(api_client: AsyncClient) -> None:
    """The default seed tenant has a company with txns — must 409."""
    async with AsyncSessionLocal() as s:
        # Pick the first non-empty company in the test DB
        cid: uuid.UUID | None = None
        for table in ("invoices", "bills", "payments", "contacts"):
            row = (
                await s.execute(
                    text(
                        f"SELECT company_id FROM {table} "  # noqa: S608 — table whitelisted
                        "WHERE company_id IS NOT NULL LIMIT 1"
                    )
                )
            ).first()
            if row is not None:
                cid = row[0]
                break
    if cid is None:
        pytest.skip("Test DB has no company with linked rows")

    r = await api_client.delete(
        f"/api/v1/companies/{cid}?hard=true",
        headers={"X-Admin": "true"},
    )
    assert r.status_code == 409, r.text
    body = r.json()
    assert "blocking_refs" in body
    assert any(v > 0 for v in body["blocking_refs"].values())


async def test_company_hard_delete_no_admin_403(api_client: AsyncClient) -> None:
    cid = await _make_clean_company()
    r = await api_client.delete(f"/api/v1/companies/{cid}?hard=true")
    assert r.status_code == 403


async def test_company_delete_without_hard_400(api_client: AsyncClient) -> None:
    """DELETE on companies without ?hard=true is 400 — soft delete not exposed."""
    cid = await _make_clean_company()
    r = await api_client.delete(
        f"/api/v1/companies/{cid}", headers={"X-Admin": "true"}
    )
    assert r.status_code == 400
