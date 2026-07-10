"""Contract tests for GET /api/v1/admin/audit-snapshots* (Wave C, FLAG_AUDIT_SNAPSHOTS).

Covers:
* admin-only (403 without X-Admin, mirrors /admin/audit-log)
* FLAG_AUDIT_SNAPSHOTS gate — 404 below Pro
* 200 + correct shape at Pro+
* tenant scoping — a snapshot belonging to another tenant is invisible
  via browse and 404s (not 403) on direct fetch
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.audit_snapshot import AuditSnapshot
from saebooks.models.tenant import Tenant

pytestmark = pytest.mark.postgres_only


@pytest.fixture
async def admin_api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}", "X-Admin": "true"},
    ) as ac:
        yield ac


@pytest.fixture
async def own_tenant_snapshot() -> uuid.UUID:
    """A snapshot row under the default tenant (the dev bearer's tenant)."""
    snap_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            AuditSnapshot(
                id=snap_id,
                tenant_id=DEFAULT_TENANT_ID,
                table_name="accounts",
                row_id=str(uuid.uuid4()),
                action="update",
                before_data={"name": "before"},
                after_data={"name": "after"},
                performed_by="test-wave-c",
            )
        )
        await session.commit()
    yield snap_id
    async with AsyncSessionLocal() as session:
        await session.execute(
            AuditSnapshot.__table__.delete().where(AuditSnapshot.id == snap_id)
        )
        await session.commit()


@pytest.fixture
async def foreign_tenant_snapshot() -> uuid.UUID:
    """A snapshot row under a DIFFERENT tenant — must never be visible
    to the default-tenant admin client."""
    foreign_tid = uuid.uuid4()
    snap_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Tenant(id=foreign_tid, name=f"foreign-{foreign_tid.hex[:6]}", slug=f"foreign-{foreign_tid.hex[:6]}")
        )
        await session.flush()
        session.add(
            AuditSnapshot(
                id=snap_id,
                tenant_id=foreign_tid,
                table_name="accounts",
                row_id=str(uuid.uuid4()),
                action="update",
                before_data={"name": "foreign-before"},
            )
        )
        await session.commit()
    yield snap_id
    async with AsyncSessionLocal() as session:
        await session.execute(
            AuditSnapshot.__table__.delete().where(AuditSnapshot.id == snap_id)
        )
        await session.execute(Tenant.__table__.delete().where(Tenant.id == foreign_tid))
        await session.commit()


async def test_audit_snapshots_requires_admin() -> None:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        r = await ac.get("/api/v1/admin/audit-snapshots")
    assert r.status_code == 403


async def test_audit_snapshots_404_below_pro(
    admin_api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saebooks.config import settings as module_settings
    monkeypatch.setattr(module_settings, "edition", "business")

    r = await admin_api_client.get("/api/v1/admin/audit-snapshots")
    assert r.status_code == 404


async def test_audit_snapshots_200_at_pro(
    admin_api_client: AsyncClient,
    own_tenant_snapshot: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saebooks.config import settings as module_settings
    monkeypatch.setattr(module_settings, "edition", "pro")

    r = await admin_api_client.get("/api/v1/admin/audit-snapshots?table_name=accounts")
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {item["id"] for item in body["items"]}
    assert str(own_tenant_snapshot) in ids


async def test_audit_snapshots_tenant_scoped_browse_excludes_foreign(
    admin_api_client: AsyncClient,
    foreign_tenant_snapshot: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saebooks.config import settings as module_settings
    monkeypatch.setattr(module_settings, "edition", "pro")

    r = await admin_api_client.get("/api/v1/admin/audit-snapshots?limit=500")
    assert r.status_code == 200, r.text
    ids = {item["id"] for item in r.json()["items"]}
    assert str(foreign_tenant_snapshot) not in ids


async def test_audit_snapshots_get_by_id_404_not_403_cross_tenant(
    admin_api_client: AsyncClient,
    foreign_tenant_snapshot: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row genuinely exists (in another tenant) — must 404, not
    leak its existence via a 403."""
    from saebooks.config import settings as module_settings
    monkeypatch.setattr(module_settings, "edition", "pro")

    r = await admin_api_client.get(f"/api/v1/admin/audit-snapshots/{foreign_tenant_snapshot}")
    assert r.status_code == 404


async def test_audit_snapshots_get_by_id_200_own_tenant(
    admin_api_client: AsyncClient,
    own_tenant_snapshot: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saebooks.config import settings as module_settings
    monkeypatch.setattr(module_settings, "edition", "pro")

    r = await admin_api_client.get(f"/api/v1/admin/audit-snapshots/{own_tenant_snapshot}")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == str(own_tenant_snapshot)


async def test_audit_snapshots_filter_options_gate(
    admin_api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saebooks.config import settings as module_settings
    monkeypatch.setattr(module_settings, "edition", "community")
    r = await admin_api_client.get("/api/v1/admin/audit-snapshots/_filter_options")
    assert r.status_code == 404

    monkeypatch.setattr(module_settings, "edition", "pro")
    r2 = await admin_api_client.get("/api/v1/admin/audit-snapshots/_filter_options")
    assert r2.status_code == 200, r2.text
    assert "tables" in r2.json() and "actors" in r2.json()
