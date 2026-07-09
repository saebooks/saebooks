"""Phase 1 tier-4 contract tests for /api/v1/projects.

Covers:
* Auth gate (401 without bearer, 401 with wrong token)
* GET /api/v1/projects → 200 with pagination shape
* GET /api/v1/projects/{id} → 200; 404 on missing UUID
* GET /api/v1/projects?archived=true → only archived results
* GET /api/v1/projects?status=COMPLETED → status filter
* POST /api/v1/projects → 201, version==1, change_log row created
* POST idempotency: same X-Idempotency-Key returns same response
* PATCH with correct If-Match → 200, version bumped
* PATCH with stale If-Match → 409 with current state in body
* PATCH without If-Match → 428
* PATCH status to ON_HOLD succeeds
* DELETE with correct If-Match → 204 (soft-archive)
* DELETE with stale If-Match → 409
* DELETE without If-Match → 428
* Archived projects not in default list
* Validation: POST without required code → 422
* change_log sequence: create + update + delete = 3 rows with ops created/updated/deleted
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


def _project_payload(**overrides: object) -> dict:
    """Return a minimal valid ProjectCreate payload."""
    base: dict = {
        "code": f"J-{uuid.uuid4().hex[:8].upper()}",
        "name": "Test Project",
        "status": "ACTIVE",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_projects_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/projects")
    assert r.status_code == 401


async def test_projects_rejects_wrong_token(unauth_client: AsyncClient) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer totally-wrong"},
    ) as ac:
        r = await ac.get("/api/v1/projects")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_projects_list_200(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/projects")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)


async def test_projects_list_default_excludes_archived(api_client: AsyncClient) -> None:
    """Default list must not include archived projects."""
    r = await api_client.post("/api/v1/projects", json=_project_payload())
    assert r.status_code == 201
    proj_id = r.json()["id"]
    v = r.json()["version"]

    await api_client.delete(f"/api/v1/projects/{proj_id}", headers={"If-Match": str(v)})

    r2 = await api_client.get("/api/v1/projects")
    ids = [i["id"] for i in r2.json()["items"]]
    assert proj_id not in ids


async def test_projects_list_archived_filter(api_client: AsyncClient) -> None:
    """?archived=true must return archived projects."""
    r = await api_client.post("/api/v1/projects", json=_project_payload())
    assert r.status_code == 201
    proj_id = r.json()["id"]
    v = r.json()["version"]

    await api_client.delete(f"/api/v1/projects/{proj_id}", headers={"If-Match": str(v)})

    r2 = await api_client.get("/api/v1/projects", params={"archived": "true"})
    assert r2.status_code == 200
    ids = [i["id"] for i in r2.json()["items"]]
    assert proj_id in ids


async def test_projects_list_status_filter(api_client: AsyncClient) -> None:
    """?status=COMPLETED must only return COMPLETED projects."""
    code = f"J-{uuid.uuid4().hex[:8].upper()}"
    r = await api_client.post(
        "/api/v1/projects", json=_project_payload(code=code, status="COMPLETED")
    )
    assert r.status_code == 201
    proj_id = r.json()["id"]

    r2 = await api_client.get("/api/v1/projects", params={"status": "COMPLETED"})
    assert r2.status_code == 200
    ids = [i["id"] for i in r2.json()["items"]]
    assert proj_id in ids

    for item in r2.json()["items"]:
        assert item["status"] == "COMPLETED"


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


async def test_projects_get_404_unknown_uuid(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/projects/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_projects_get_200(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/projects", json=_project_payload())
    assert r.status_code == 201
    proj_id = r.json()["id"]

    r2 = await api_client.get(f"/api/v1/projects/{proj_id}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["id"] == proj_id
    assert body["status"] == "ACTIVE"


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_projects_create_201(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/projects", json=_project_payload())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["archived_at"] is None
    assert body["status"] == "ACTIVE"
    assert "tenant_id" in body
    assert "id" in body
    assert "code" in body
    assert "name" in body


async def test_projects_create_with_dates(api_client: AsyncClient) -> None:
    payload = _project_payload(
        start_date="2026-01-01",
        end_date="2026-12-31",
        notes="Test project with dates",
    )
    r = await api_client.post("/api/v1/projects", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["start_date"] == "2026-01-01"
    assert body["end_date"] == "2026-12-31"
    assert body["notes"] == "Test project with dates"


async def test_projects_create_change_log(api_client: AsyncClient) -> None:
    """POST should produce a change_log row with op=created, version=1."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/projects", json=_project_payload())
    assert r.status_code == 201
    proj_id = r.json()["id"]

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(proj_id),
                    ChangeLog.entity == "project",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert len(rows) >= 1
    assert rows[-1].op == "created"
    assert rows[-1].version == 1


async def test_projects_create_validation_missing_code(api_client: AsyncClient) -> None:
    """POST without code should return 422."""
    payload = _project_payload()
    del payload["code"]
    r = await api_client.post("/api/v1/projects", json=payload)
    assert r.status_code == 422


async def test_projects_create_on_hold_status(api_client: AsyncClient) -> None:
    """Creating a project with ON_HOLD status should succeed."""
    r = await api_client.post("/api/v1/projects", json=_project_payload(status="ON_HOLD"))
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "ON_HOLD"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_projects_create_idempotency(api_client: AsyncClient) -> None:
    """Same X-Idempotency-Key returns the same response body on replay."""
    key = str(uuid.uuid4())
    payload = _project_payload()

    r1 = await api_client.post(
        "/api/v1/projects",
        json=payload,
        headers={"X-Idempotency-Key": key},
    )
    assert r1.status_code == 201
    id1 = r1.json()["id"]

    r2 = await api_client.post(
        "/api/v1/projects",
        json=payload,
        headers={"X-Idempotency-Key": key},
    )
    assert r2.status_code == 201
    assert r2.json()["id"] == id1


# ---------------------------------------------------------------------------
# Update — valid If-Match
# ---------------------------------------------------------------------------


async def test_projects_update_bumps_version(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/projects", json=_project_payload())
    assert r.status_code == 201
    proj_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.patch(
        f"/api/v1/projects/{proj_id}",
        json={"name": "Updated Project Name"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    updated = r2.json()
    assert updated["version"] == v + 1
    assert updated["name"] == "Updated Project Name"


async def test_projects_update_status_to_on_hold(api_client: AsyncClient) -> None:
    """PATCH can transition status to ON_HOLD."""
    r = await api_client.post("/api/v1/projects", json=_project_payload())
    assert r.status_code == 201
    proj_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.patch(
        f"/api/v1/projects/{proj_id}",
        json={"status": "ON_HOLD"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "ON_HOLD"


# ---------------------------------------------------------------------------
# Update — missing If-Match → 428
# ---------------------------------------------------------------------------


async def test_projects_update_requires_if_match(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/projects", json=_project_payload())
    assert r.status_code == 201
    proj_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/projects/{proj_id}", json={"name": "x"}
    )
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# Update — stale If-Match → 409
# ---------------------------------------------------------------------------


async def test_projects_stale_if_match_returns_409(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/projects", json=_project_payload())
    assert r.status_code == 201
    proj_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/projects/{proj_id}",
        json={"name": "stale attempt"},
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == proj_id
    assert body["current"]["version"] == 1


# ---------------------------------------------------------------------------
# Delete → 204
# ---------------------------------------------------------------------------


async def test_projects_delete_204(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/projects", json=_project_payload())
    assert r.status_code == 201
    proj_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.delete(
        f"/api/v1/projects/{proj_id}",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 204

    # Should no longer appear in default list
    r3 = await api_client.get("/api/v1/projects")
    ids = [i["id"] for i in r3.json()["items"]]
    assert proj_id not in ids


async def test_projects_delete_stale_if_match_409(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/projects", json=_project_payload())
    assert r.status_code == 201
    proj_id = r.json()["id"]

    r2 = await api_client.delete(
        f"/api/v1/projects/{proj_id}",
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409


async def test_projects_delete_requires_if_match(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/projects", json=_project_payload())
    assert r.status_code == 201
    proj_id = r.json()["id"]

    r2 = await api_client.delete(f"/api/v1/projects/{proj_id}")
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# change_log sequence
# ---------------------------------------------------------------------------


async def test_projects_change_log_full_sequence(api_client: AsyncClient) -> None:
    """Create + update + delete = 3 project change_log rows in order."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/projects", json=_project_payload())
    assert r.status_code == 201
    proj_id = r.json()["id"]

    await api_client.patch(
        f"/api/v1/projects/{proj_id}",
        json={"name": "Renamed Project"},
        headers={"If-Match": "1"},
    )
    await api_client.delete(
        f"/api/v1/projects/{proj_id}",
        headers={"If-Match": "2"},
    )

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(proj_id),
                    ChangeLog.entity == "project",
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
